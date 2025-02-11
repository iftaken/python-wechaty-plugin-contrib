from __future__ import annotations
from datetime import datetime
import asyncio
from enum import Enum
from typing import Callable, Awaitable, Optional, Any
from asyncio import sleep
from logging import Logger
from dataclasses import dataclass
from dataclasses_json import dataclass_json

from wechaty import (
    WechatyPlugin,
    Wechaty,
    WechatyPluginOptions, Contact
)
from quart import Quart, jsonify
from wechaty_puppet import get_logger

# keep consist with http code


class HealthCheckerStatus(Enum):
    Ready = -1
    Success = 200
    Failed = 500
    Stopped = 1000


log: Logger = get_logger('HealthCheckPlugin')


async def _empty_async_func(*_: Any):
    pass


class HealthChecker:
    """
    Inspired by:
        * https://github.com/Kludex/fastapi-health
        * https://microservices.io/patterns/observability/health-check-api.html
    """
    def __init__(
        self,
        success_checker: Callable[..., Awaitable[bool]],

        success_handler: Optional[Callable[[HealthChecker], Awaitable]] = None,
        failure_handler: Optional[Callable[[HealthChecker], Awaitable]] = None,
        final_handler: Optional[Callable[[HealthChecker], Awaitable]] = None,

        timeout: int = 1,
        max_retry_turns: int = 3,

        final_failure_handler: Callable[..., Awaitable] = None,

        log: Optional[Logger] = None
    ):
        """
        checking application health status
        Args:
            success_checker: to check if the application is health

            success_handler: if it is success, it will call this handler
            failure_handler: if it is failed, it will call this handler
            timeout: the max timeout between success checker list

            max_retry_turns: if it fails, the checker should retry the number of turns which try to keep application alove
            final_failure_handler: if retry action is failed, it will call this handler
        """
        self.log: Logger = log

        if timeout <= 0:
            raise ValueError(f'timeout value should greater than 0')

        if timeout < 3:
            self.log.warning(
                'the timeout is too small which will make health checker crashed down. We suggest more than 10.'
            )

        # 1. init the semaphores
        self._status_code: HealthCheckerStatus = HealthCheckerStatus.Ready
        self._timeout: int = timeout
        self._max_retry_turns: int = max_retry_turns
        self._retry_turns: int = 0

        # 2. init the callable function
        self._success_checker = success_checker
        self._success_handler = success_handler or _empty_async_func
        self._failure_handler = failure_handler or _empty_async_func
        self._final_handler = final_handler or _empty_async_func

        self._final_failure_handler = final_failure_handler or _empty_async_func()

    def is_success(self) -> bool:
        """
        check if the application is success
        """
        return self._status_code == HealthCheckerStatus.Success

    async def monitor(self):
        """
        monitor the health with success/failure checkers
        """
        if self._status_code == HealthCheckerStatus.Stopped:
            return

        if self._retry_turns == 0:
            self.log.info('The application is health 💖 💖 💖')
        else:
            self.log.warning(
                'We are trying to save your application in turns<%s/%s>',
                self._retry_turns,
                self._max_retry_turns
            )

        if self._retry_turns > self._max_retry_turns:
            self.log.critical(
                'The application crashed down 💔 💔 💔, we will restart the health-checker to try activate application.'
            )
            self._status_code = HealthCheckerStatus.Failed
            await self._final_failure_handler(self)
            return

        # self._status_code = HealthCheckerStatus.Success
        is_health: bool = await self._success_checker()
        if not is_health:

            log.info('The application is not health 💔, we are trying to save it.')
            self._status_code = HealthCheckerStatus.Failed
            await self._failure_handler(self)
            self._retry_turns += 1

        else:
            self._status_code = HealthCheckerStatus.Success
            self._retry_turns = 0
            await self._success_handler(self)

        await self._final_handler(self)
        await sleep(self._timeout)
        await self.monitor()

    async def stop(self):
        self._status_code = HealthCheckerStatus.Stopped

    async def start(self):
        self._status_code = HealthCheckerStatus.Ready
        await self.monitor()

    async def restart(self):
        await self.stop()
        await self.start()


@dataclass_json
@dataclass
class HealthCheckPluginOptions(WechatyPluginOptions):
    """
    options for HealthCheckPlugin
    """
    max_retry_turns: int = 10   # when failed, max retrying times
    timeout: int = 60           # make ding-dong testing in every 60 seconds
    success_handler: Optional[Callable[[HealthChecker], Awaitable]] = None
    failure_handler: Optional[Callable[[HealthChecker], Awaitable]] = None
    final_handler: Optional[Callable[[HealthChecker], Awaitable]] = None


class DingDongStatus(Enum):
    Start = 0
    Waiting = 1
    Received = 2


class HealthCheckPlugin(WechatyPlugin):
    """
    Health Checking Plugin which aims to keep wechaty instance alive.
    """
    def __init__(self, options: Optional[HealthCheckPluginOptions] = None):
        options = options or HealthCheckPluginOptions()
        super().__init__(options=options)

        self.options = options
        self.health_checker = HealthChecker(
            success_checker=self.check_wechaty_is_health,
            success_handler=options.success_handler,
            failure_handler=options.failure_handler,

            max_retry_turns=options.max_retry_turns,
            timeout=options.timeout,
            log=log
        )

        self._ding_dong_status: DingDongStatus = DingDongStatus.Start

    async def on_dong(self, *_: Any):
        """
        set the event and let handle
        Args:
            *_: the data of dong event
        """
        self._ding_dong_status = DingDongStatus.Received

    async def check_wechaty_is_health(self) -> bool:
        """
        send ding info to the server and wait for dong event to make sure that the service is health

        Returns: if the wechaty bot is health
        """

        if not self.bot:
            raise ValueError('the wechaty instance is none, which is critical error')
        self._ding_dong_status = DingDongStatus.Start

        # 1. send ding info to the service
        await self.bot.puppet.ding()
        self._ding_dong_status = DingDongStatus.Waiting

        # 2. wait for <timeout> seconds to check the semaphore
        for _ in range(self.options.timeout):
            if self._ding_dong_status == DingDongStatus.Received:
                return True
            await sleep(1)

        # 3. timeout for receive ding data, so return False
        return False

    async def init_plugin(self, wechaty: Wechaty) -> None:
        await super(HealthCheckPlugin, self).init_plugin(wechaty=wechaty)

        wechaty.on('dong', self.on_dong)

        # pend the health checker task to the event loop
        loop = asyncio.get_event_loop()

        asyncio.run_coroutine_threadsafe(
            self.health_checker.start(),
            loop=loop
        )

    async def blueprint(self, app: Quart) -> None:

        @app.route('/health')
        def get_health_status():
            is_health = self.health_checker.is_success()

            if is_health:
                msg = 'The application is health 💖 💖 💖'
            else:
                msg = 'The application crashed down 💔 💔 💔'

            return jsonify({
                "code": 200,
                "msg": msg,
                "is_health": is_health
            })

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import asyncio
import logging
from typing import Optional, Union

from wechaty_puppet import FileBox

from wechaty import Wechaty, Contact
from wechaty.user import Message, Room

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(filename)s <%(funcName)s> %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

async def message(msg: Message) -> None:
    """back on message"""
    from_contact = msg.talker()
    text = msg.text()
    room = msg.room()
    if text == 'ding':
        conversation: Union[
            Room, Contact] = from_contact if room is None else room
        await conversation.ready()
        await conversation.say('dong')
        file_box = FileBox.from_url(
            'https://ss3.bdstatic.com/70cFv8Sh_Q1YnxGkpoWK1HF6hhy/it/'
            'u=1116676390,2305043183&fm=26&gp=0.jpg',
            name='ding-dong.jpg')
        await conversation.say(file_box)

bot: Optional[Wechaty] = None


class Counter:
    def __init__(self, wechaty: Wechaty):
        self.wechaty = wechaty
        self.count = 0

    async def step(self):
        self.count += 1
        log.info("Ding Dong Step Event ...")
        if self.count == 50:
            contact = await self.wechaty.Contact.find('秋客')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S %f')
            await contact.say(f'I"m alive - {now}')
            self.count = 0


async def main() -> None:
    """doc"""
    # pylint: disable=W0603
    global bot
    bot = Wechaty().on('message', message)

    import os
    os.environ['token'] = 'wujingjing-ubuntu-server-padlocal-token'
    counter = Counter(bot)
    plugin_options = HealthCheckPluginOptions()
    plugin_options.success_handler = counter.step

    health_check_plugin = HealthCheckPlugin(
        options=plugin_options
    )

    bot.use(health_check_plugin)
    await bot.start()


if __name__ == '__main__':
    asyncio.run(main())
