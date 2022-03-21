import asyncio
import copy
import os
import shutil
from typing import TYPE_CHECKING, Dict, Optional

from pydantic import ValidationError

from hummingbot.client.config.config_data_types import BaseClientModel, BaseStrategyConfigMap
from hummingbot.client.config.config_helpers import (
    default_strategy_file_path,
    format_config_file_name,
    get_strategy_config_map,
    get_strategy_template_path,
    parse_config_default_to_text,
    parse_cvar_value,
    retrieve_validation_error_msg,
    save_to_yml,
    save_to_yml_legacy
)
from hummingbot.client.config.config_var import ConfigVar
from hummingbot.client.config.global_config_map import global_config_map
from hummingbot.client.config.security import Security
from hummingbot.client.settings import CONF_FILE_PATH, required_exchanges
from hummingbot.client.ui.completer import load_completer
from hummingbot.core.utils.async_utils import safe_ensure_future

if TYPE_CHECKING:
    from hummingbot.client.hummingbot_application import HummingbotApplication


class CreateCommand:
    def create(self,  # type: HummingbotApplication
               file_name):
        if file_name is not None:
            file_name = format_config_file_name(file_name)
            if os.path.exists(os.path.join(CONF_FILE_PATH, file_name)):
                self._notify(f"{file_name} already exists.")
                return

        safe_ensure_future(self.prompt_for_configuration(file_name))

    async def prompt_for_configuration(
        self,  # type: HummingbotApplication
        file_name,
    ):
        self.app.clear_input()
        self.placeholder_mode = True
        self.app.hide_input = True
        required_exchanges.clear()

        strategy = await self.get_strategy_name()

        if self.app.to_stop_config:
            self.stop_config()
            return

        config_map = get_strategy_config_map(strategy)
        self._notify(f"Please see https://docs.hummingbot.io/strategies/{strategy.replace('_', '-')}/ "
                     f"while setting up these below configuration.")

        if isinstance(config_map, BaseStrategyConfigMap):
            await self.prompt_for_model_config(config_map)
        elif config_map is not None:
            await self.prompt_for_configuration_legacy(file_name, strategy, config_map)
            self.app.to_stop_config = True
        else:
            self.app.to_stop_config = True

        if self.app.to_stop_config:
            self.stop_config()
            return

        file_name = await self.save_config_to_file(file_name, config_map)
        self.strategy_file_name = file_name
        self.strategy_name = strategy
        self.strategy_config_map = config_map
        # Reload completer here otherwise the new file will not appear
        self.app.input_field.completer = load_completer(self)
        self.placeholder_mode = False
        self.app.hide_input = False

        await self.verify_status()

    async def get_strategy_name(
        self,  # type: HummingbotApplication
    ) -> Optional[str]:
        strategy = None
        strategy_config = BaseStrategyConfigMap.construct()
        await self.prompt_for_model_config(strategy_config)
        if self.app.to_stop_config:
            self.stop_config()
        else:
            strategy = strategy_config.strategy
        return strategy

    async def prompt_for_model_config(
        self,  # type: HummingbotApplication
        config_map: BaseClientModel,
    ):
        for key, field in config_map.__fields__.items():
            client_data = config_map.get_client_data(key)
            if (
                client_data is not None
                and (client_data.prompt_on_new and field.required)
            ):
                await self.prompt_a_config(config_map, key)
                if self.app.to_stop_config:
                    break

    async def prompt_for_configuration_legacy(
        self,  # type: HummingbotApplication
        file_name,
        strategy: str,
        config_map: Dict,
    ):
        config_map_backup = copy.deepcopy(config_map)
        # assign default values and reset those not required
        for config in config_map.values():
            if config.required:
                config.value = config.default
            else:
                config.value = None
        for config in config_map.values():
            if config.prompt_on_new and config.required:
                if not self.app.to_stop_config:
                    await self.prompt_a_config_legacy(config)
                else:
                    break
            else:
                config.value = config.default

        if self.app.to_stop_config:
            self.stop_config(config_map, config_map_backup)
            return

        if file_name is None:
            file_name = await self.prompt_new_file_name(strategy)
            if self.app.to_stop_config:
                self.stop_config(config_map, config_map_backup)
                self.app.set_text("")
                return
        self.app.change_prompt(prompt=">>> ")
        strategy_path = os.path.join(CONF_FILE_PATH, file_name)
        template = get_strategy_template_path(strategy)
        shutil.copy(template, strategy_path)
        save_to_yml_legacy(strategy_path, config_map)
        self.strategy_file_name = file_name
        self.strategy_name = strategy
        self.strategy_config = None
        # Reload completer here otherwise the new file will not appear
        self.app.input_field.completer = load_completer(self)
        self._notify(f"A new config file {self.strategy_file_name} created.")
        self.placeholder_mode = False
        self.app.hide_input = False

        await self.verify_status()

    async def prompt_a_config(
        self,  # type: HummingbotApplication
        model: BaseClientModel,
        config: str,
        input_value=None,
    ):
        config_path = config.split(".")
        while len(config_path) != 1:
            sub_model_attr = config_path.pop(0)
            model = model.__getattribute__(sub_model_attr)
        config = config_path[0]
        if input_value is None:
            prompt = await model.get_client_prompt(config)
            if prompt is not None:
                prompt = f"{prompt} >>> "
                client_data = model.get_client_data(config)
                input_value = await self.app.prompt(prompt=prompt, is_password=client_data.is_secure)

        new_config_value = None
        if not self.app.to_stop_config and input_value is not None:
            try:
                model.__setattr__(config, input_value)
                new_config_value = model.__getattribute__(config)
            except ValidationError as e:
                err_msg = retrieve_validation_error_msg(e)
                self._notify(err_msg)
                new_config_value = await self.prompt_a_config(model, config)

        if not self.app.to_stop_config and isinstance(new_config_value, BaseClientModel):
            await self.prompt_for_model_config(new_config_value)

    async def prompt_a_config_legacy(
        self,  # type: HummingbotApplication
        config: ConfigVar,
        input_value=None,
        assign_default=True,
    ):
        if config.key == "inventory_price":
            await self.inventory_price_prompt_legacy(self.strategy_config_map, input_value)
            return
        if input_value is None:
            if assign_default:
                self.app.set_text(parse_config_default_to_text(config))
            prompt = await config.get_prompt()
            input_value = await self.app.prompt(prompt=prompt, is_password=config.is_secure)

        if self.app.to_stop_config:
            return
        value = parse_cvar_value(config, input_value)
        err_msg = await config.validate(input_value)
        if err_msg is not None:
            self._notify(err_msg)
            await self.prompt_a_config_legacy(config)
        else:
            config.value = value

    async def save_config_to_file(
        self,  # type: HummingbotApplication
        file_name: Optional[str],
        config_map: BaseStrategyConfigMap,
    ) -> str:
        if file_name is None:
            file_name = await self.prompt_new_file_name(config_map.strategy)
            if self.app.to_stop_config:
                self.stop_config()
                self.app.set_text("")
                return
        self.app.change_prompt(prompt=">>> ")
        strategy_path = os.path.join(CONF_FILE_PATH, file_name)
        save_to_yml(strategy_path, config_map)
        return file_name

    async def prompt_new_file_name(self,  # type: HummingbotApplication
                                   strategy):
        file_name = default_strategy_file_path(strategy)
        self.app.set_text(file_name)
        input = await self.app.prompt(prompt="Enter a new file name for your configuration >>> ")
        input = format_config_file_name(input)
        file_path = os.path.join(CONF_FILE_PATH, input)
        if input is None or input == "":
            self._notify("Value is required.")
            return await self.prompt_new_file_name(strategy)
        elif os.path.exists(file_path):
            self._notify(f"{input} file already exists, please enter a new name.")
            return await self.prompt_new_file_name(strategy)
        else:
            return input

    async def update_all_secure_configs_legacy(
        self  # type: HummingbotApplication
    ):
        await Security.wait_til_decryption_done()
        Security.update_config_map(global_config_map)
        if self.strategy_config_map is not None and not isinstance(self.strategy_config_map, BaseStrategyConfigMap):
            Security.update_config_map(self.strategy_config_map)

    async def verify_status(
        self  # type: HummingbotApplication
    ):
        try:
            timeout = float(global_config_map["create_command_timeout"].value)
            all_status_go = await asyncio.wait_for(self.status_check_all(), timeout)
        except asyncio.TimeoutError:
            self._notify("\nA network error prevented the connection check to complete. See logs for more details.")
            self.strategy_file_name = None
            self.strategy_name = None
            self.strategy_config = None
            raise
        if all_status_go:
            self._notify("\nEnter \"start\" to start market making.")

    def stop_config(
        self,
        config_map: Optional[Dict[str, ConfigVar]] = None,
        config_map_backup: Optional[Dict[str, ConfigVar]] = None,
    ):
        if config_map is not None and config_map_backup is not None:
            self.restore_config_legacy(config_map, config_map_backup)
        self.app.to_stop_config = False

    @staticmethod
    def restore_config_legacy(config_map: Dict[str, ConfigVar], config_map_backup: Dict[str, ConfigVar]):
        for key in config_map:
            config_map[key] = config_map_backup[key]
