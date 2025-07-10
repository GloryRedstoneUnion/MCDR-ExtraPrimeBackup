import json
import os
import re
import threading
from copy import deepcopy, copy
from queue import Queue, Empty
from threading import RLock
from types import MethodType
from typing import Optional, Dict
import functools, inspect
from mcdreforged.api.all import *
from mcdreforged.plugin.type.plugin import AbstractPlugin
from mcdreforged.command.builder.nodes.basic import Callable
import time
# noinspection PyUnresolvedReferences
import minecraft_data_api as api

# ---------- Config ---------
PBCHECKPOINT = os.path.join('check_point.json')
PlServer: PluginServerInterface = None
# 配置项：覆写模式，thread=线程守护，event=事件触发
DEFAULT_OVERRIDE_MODE = 'thread'  # 可选 'thread' 或 'event'


class ParseConfig(Serializable):
    block_info_command: str = 'info block get {x} {y} {z}'
    block_info_regex: re.Pattern = re.compile(r"Block info for (?P<block>minecraft:[\w_]+),")
    block_value_regex: re.Pattern = re.compile(r"(\w+)=([A-Z_]+|\w+)")


class PbCheckPoint(Serializable):
    check_point: dict = {}
    override_mode: str = "event"


CP_CONFIG: PbCheckPoint


def save_config(path: str = PBCHECKPOINT):
    PlServer.save_config_simple(CP_CONFIG, path)


# ---------- InfoManager ---------
class BlockInfoGetter:
    ALLOWED_WORLDS = {"overworld", "the_nether", "the_end"}
    def __init__(self, server: PluginServerInterface):
        self.server: PluginServerInterface = server
        self.block_name: str = ''
        self.block_data: dict = {}
        self.__TIMEOUT = 0.8
        self._lock = threading.Lock()

    def on_info(self, info: Info):
        if not info.is_user:
            if (m := ParseConfig.block_info_regex.search(info.content)) is not None:
                if self.block_name == '':
                    self.server.logger.info('block entity data output match found: {}'.format(m.groupdict()))
                    self.block_name = m.group('block')
                    self.block_data = {key: val for key, val in ParseConfig.block_value_regex.findall(info.content)}
                    self.server.logger.info('block entity data output match found: {}'.format(self.block_data))

    def get_block_info(self, x, y, z, world):
        world = str(world).lower()
        if world not in self.ALLOWED_WORLDS:
            self.server.logger.warning(f'[ExtraPrimeBackup] world参数非法: {world}，仅支持 overworld/the_nether/the_end')
            return False

        # 清空之前的数据
        self.block_name = ''
        self.block_data = {}

        ti = time.time()
        self.server.logger.info(f'获取方块信息: {x} {y} {z} in {world}')
        self.server.execute(f'/execute in minecraft:{world} run info block {x} {y} {z}')

        # 等待数据
        while self.block_name == '' and time.time() - ti <= self.__TIMEOUT:
            time.sleep(0.05)

        self.server.logger.warning(f'block_name: {self.block_name}, block_data: {self.block_data}')
        return True if self.block_name == '' else False


block_info_getter: Optional[BlockInfoGetter] = None


def on_info(server: PluginServerInterface, info):
    if block_info_getter:
        block_info_getter.on_info(info)


# ---------- Command ---------

def cmd_help(source: CommandSource, context: dict):
    what = context.get('what')
    # if what is not None and what not in ShowHelpTask.COMMANDS_WITH_DETAILED_HELP:
    # reply_message(source, tr('command.help.no_help', RText(mkcmd(what), RColor.gray)))
    help_message = """
    §aExtraPrimeBackup 指令帮助:
    §e!!pb cp help §7- 显示此帮助信息
    §e!!pb cp list §7- 列出所有检查点
    §e!!pb cp status <name> §7- 显示指定检查点的状态
    §e!!pb cp del <name> §7- 删除指定检查点
    §e!!pb cp add <x> <y> <z> <name> §7- 添加新的检查点
    §e!!pb ignore §7- 忽略检查点状态强制执行
    """
    source.reply(help_message)
    return

    # self.task_manager.add_task(ShowHelpTask(source, what))


def cmd_list(source: CommandSource, context: dict):
    for index in CP_CONFIG.check_point:
        source.reply(f'§e{index} §7-> §a{CP_CONFIG.check_point[index]}')


@new_thread('Pb_CheckPoint_Status')
def cmd_status(source: CommandSource, context: dict):
    if CP_CONFIG.check_point.get(context['name'], None) is not None:
        pei: Dict = CP_CONFIG.check_point[context["name"]]
        source.reply(f'§e{context["name"]} §7-> §a{pei}')
        world = pei.get('world', 'overworld')  # 兼容旧数据，默认overworld
        if block_info_getter.get_block_info(pei['x'], pei['y'], pei['z'], world):
            source.reply('§c未能获取方块信息')
            return
        source.reply(f'§a机器处于 §e关闭 §a状态' if block_info_getter.block_data == pei['data'] and block_info_getter.block_name == pei['block'] else '§c机器处于 §e开启 §c状态')
    else:
        source.reply('§c配置不存在')


def cmd_del(source: CommandSource, context: dict):
    if CP_CONFIG.check_point.get(context['name'], None) is not None:
        del CP_CONFIG.check_point[context['name']]
        source.reply('§a删除成功')
        return
    else:
        source.reply('§e配置不存在')


@new_thread('Pb_CheckPoint_Add')
def cmd_add(source: CommandSource, context: dict):
    # 检查名字是否已存在
    if CP_CONFIG.check_point.get(context['name'], None) is not None:
        source.reply('§c该名字已被使用')
        return
    # world参数处理
    world = context.get('world')
    if not world:
        if hasattr(source, 'player') and source.player:
            try:
                world = api.get_player_dimension(source.player)
            except Exception as e:
                source.reply('§c自动获取玩家维度失败，请手动指定 world (overworld/the_nether/the_end)')
                return
        else:
            source.reply('§c未指定 world，且无法自动获取玩家维度，请手动指定 world (overworld/the_nether/the_end)')
            return
    world = str(world).lower()
    if world not in BlockInfoGetter.ALLOWED_WORLDS:
        source.reply('§cworld参数非法，仅支持 overworld/the_nether/the_end')
        return
    context['world'] = world
    # 获取方块信息
    if block_info_getter.get_block_info(context['x'], context['y'], context['z'], world):
        source.reply('§c未能获取方块信息')
        return
    jsondata = json.loads(CP_CONFIG.check_point.get(context.get('json', '{}'), '{}'))
    CP_CONFIG.check_point[context['name']] = {
        'x': context['x'],
        'y': context['y'],
        'z': context['z'],
        'world': world,
        'block': block_info_getter.block_name,
        'data': jsondata if jsondata != {} else block_info_getter.block_data
    }
    save_config()
    source.reply('§a添加成功')


def check(source: CommandSource, group=False):
    if group:
        lis = ""
    f = 1
    for index in CP_CONFIG.check_point:
        time.sleep(0.2)
        world = CP_CONFIG.check_point[index].get('world', 'overworld')  # 兼容旧数据，默认overworld
        if block_info_getter.get_block_info(CP_CONFIG.check_point[index]['x'], CP_CONFIG.check_point[index]['y'],
                                            CP_CONFIG.check_point[index]['z'], world):
            if not group:
                source.reply(f'§c未能获取机器 §e{index} 的状态')
            f = 0
            continue
        if block_info_getter.block_name != CP_CONFIG.check_point[index]['block'] or block_info_getter.block_data != \
                CP_CONFIG.check_point[index]['data']:
            if group:
                lis += index + ','
            if not group:
                source.get_server().broadcast(f'§c机器 §e{index} §c貌似没有关闭')
            f = 0
    if group:
        return lis
    if f:
        return False
    return True


help_callback = None
make_callback = None
override_monitor_thread = None
override_monitor_running = False
override_monitor_lock = threading.Lock()


def help_callback_override(source: CommandSource, context: CommandContext):
    global CP_CONFIG, block_info_getter  # 确保使用当前插件实例
    source.reply('§epb已被入注，使用!!pb cp观看入注内容')
    help_callback(source, context)


@new_thread('Pb_CheckPoint_Check')
def make_callback_override(source: CommandSource, context: CommandContext, ignore=True):
    global CP_CONFIG, block_info_getter  # 确保使用当前插件实例
    if check(source) and ignore:
        source.get_server().broadcast("§e请关闭所有机器后再次确定，或者使用 !!pb ignore 强制执行")
        return
    if not ignore:
        if context.get('comment', None) is None:
            context['comment'] = f'§e强制备份 未关机机器(§c{check(source, True)}§e)'
        else:
            context['comment'] = context['comment'] + f' §e强制备份 未关机机器(§c{check(source, True)}§e)'
        make_callback(source, context)
    else:
        make_callback(source, context)


def extract_function_name(func_str):
    match = re.match(r"<function (.*?) at", str(func_str))
    if match:
        return match.group(1)
    return None


def monitor_and_override_primebackup(server, builder, timeout=None):
    global help_callback, make_callback, override_monitor_running
    with override_monitor_lock:
        override_monitor_running = True
        server.logger.info('[ExtraPrimeBackup] 启动覆写监控线程')
        start_time = time.time()
        while override_monitor_running:
            try:
                pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
                node = pl.mcdr_server.command_manager.root_nodes.get('!!pb', [None])[0]
                if node is not None:
                    make_node = node.node._children_literal.get('make', [None])[0]
                    if make_node is not None:
                        # 检查是否已被覆写，或者强制重新覆写以确保指向当前插件实例
                        current_callback_name = extract_function_name(getattr(make_node, '_callback', ''))
                        if (current_callback_name != extract_function_name(make_callback_override) or
                                make_callback is None):
                            make_callback = copy(getattr(make_node, '_callback', None))
                            builder.add_children_for(node.node)
                            help_callback = copy(getattr(node.node, '_callback', None))
                            builder.add_children_for(node.node)
                            make_node._callback = make_callback_override
                            node.node._callback = help_callback_override
                            server.logger.info('[ExtraPrimeBackup] 覆写 primebackup 指令成功')
            except Exception as e:
                server.logger.warning(f'[ExtraPrimeBackup] 覆写 primebackup 指令异常: {e}')
            # 检查超时
            if timeout is not None and (time.time() - start_time) > timeout:
                server.logger.info(f'[ExtraPrimeBackup] 事件模式线程已到达 {timeout}s，自动退出')
                break
            # 使用短时间间隔检测停止标志，提高响应性
            for _ in range(10):  # 总共等待1秒，但每0.1秒检查一次停止标志
                if not override_monitor_running:
                    break
                time.sleep(0.5)
        server.logger.info('[ExtraPrimeBackup] 覆写监控线程已停止')


def on_load(server: PluginServerInterface, prev):
    global CP_CONFIG, block_info_getter, PlServer, override_monitor_thread, override_monitor_running
    block_info_getter = BlockInfoGetter(server)
    PlServer = server
    # 加载配置，增加 override_mode 选项
    CP_CONFIG = server.load_config_simple(PBCHECKPOINT, target_class=PbCheckPoint, in_data_folder=True)
    override_mode = CP_CONFIG.override_mode
    pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
    server.get_plugin_command_source()
    builder = SimpleCommandBuilder()
    for i in ['cp', 'checkpoint']:
        builder.command(i, cmd_help)
        builder.command(f'{i} help', cmd_help)
        builder.command(f'{i} help <what>', cmd_help)
        builder.arg('what', Text)
        builder.command(f'{i} list', cmd_list)
        builder.command(f'{i} ls', cmd_list)
        builder.command(f'{i} status <name>', cmd_status)
        builder.command(f'{i} st <name>', cmd_status)
        builder.command(f'{i} del <name>', cmd_del)
        builder.command(f'{i} add <x> <y> <z> <world> <name>', cmd_add)
        builder.command(f'{i} add <x> <y> <z> <name>', cmd_add)
        # builder.command(f'{i} add <x> <y> <z> <name> <json>', cmd_add)
        builder.arg('x', Integer)
        builder.arg('y', Integer)
        builder.arg('z', Integer)
        builder.arg('name', GreedyText)
        builder.arg('world', Text)
        # builder.arg('json', Text)
        builder.command('ig <comment>', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ignore <comment>', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ig', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ignore', lambda src, tex: make_callback_override(src, tex, False))
        builder.arg('comment', GreedyText)

    with override_monitor_lock:
        # 关闭旧线程
        if override_monitor_thread is not None and override_monitor_thread.is_alive():
            override_monitor_running = False
            override_monitor_thread.join(timeout=2)

        # 线程模式
        if override_mode == 'thread':
            override_monitor_thread = threading.Thread(
                target=monitor_and_override_primebackup,
                args=(server, builder),
                name="ExtraPrimeBackup_OverrideMonitor"
            )
            override_monitor_thread.daemon = True
            override_monitor_thread.start()
            server.logger.info('[ExtraPrimeBackup] 线程守护模式已启动')
        # 事件模式
        elif override_mode == 'event':
            override_monitor_thread = threading.Thread(
                target=monitor_and_override_primebackup,
                args=(server, builder, 5),  # 5秒后自动退出
                name="ExtraPrimeBackup_OverrideMonitor"
            )
            override_monitor_thread.daemon = True
            override_monitor_thread.start()
            server.logger.info('[ExtraPrimeBackup] 事件触发模式已启动（5秒自动退出）')
        else:
            server.logger.warning(f'[ExtraPrimeBackup] 未知的 override_mode: {override_mode}，不进行自动覆写')


def on_unload(server: PluginServerInterface):
    """
    插件卸载时优雅地停止监控线程
    """
    global override_monitor_running, override_monitor_thread
    with override_monitor_lock:
        if override_monitor_thread is not None and override_monitor_thread.is_alive():
            server.logger.info('[ExtraPrimeBackup] 正在停止覆写监控线程...')
            override_monitor_running = False
            override_monitor_thread.join(timeout=2)
            if override_monitor_thread.is_alive():
                server.logger.warning('[ExtraPrimeBackup] 监控线程未能在超时时间内停止')
            else:
                server.logger.info('[ExtraPrimeBackup] 监控线程已成功停止')
