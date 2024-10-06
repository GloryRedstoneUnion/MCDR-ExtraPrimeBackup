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
# ---------- Config ---------
PBCHECKPOINT = os.path.join('check_point.json')
PlServer: PluginServerInterface = None


class ParseConfig(Serializable):
    block_info_command: str = 'info block get {x} {y} {z}'
    block_info_regex: re.Pattern = re.compile(r"Block info for (?P<block>minecraft:[\w_]+)")
    block_value_regex: re.Pattern = re.compile(r"(\w+|'[^']+')=(\w+|'[^']+')")


class PbCheckPoint(Serializable):
    check_point: dict = {}


CP_CONFIG: PbCheckPoint


def save_config(path: str = PBCHECKPOINT):
    PlServer.save_config_simple(CP_CONFIG, path)


# ---------- InfoManager ---------
class BlockInfoGetter:
    def __init__(self, server: PluginServerInterface):
        self.server: PluginServerInterface = server
        self.block_name: str = ''
        self.block_data: dict = {}
        self.__TIMEOUT = 0.4

    def on_info(self, info: Info):
        if not info.is_user:
            if (m := ParseConfig.block_info_regex.match(info.content)) is not None:
                self.server.logger.info('block entity data output match found: {}'.format(m.groupdict()))
                self.block_name = m.group('block')
                self.block_data = {key: val for key, val in ParseConfig.block_value_regex.findall(info.content)}

    def get_block_info(self, x, y, z):
        self.block_name: str = ''
        self.block_data: dict = {}
        ti = time.time()
        self.server.execute(f'info block {x} {y} {z}')
        while self.block_name == '' and time.time() - ti <= self.__TIMEOUT:
            time.sleep(0.05)
        self.server.logger.warning(f'block_name: {self.block_name}, block_data: {self.block_data}')
        return True if self.block_name == '' else False


block_info_getter: Optional[BlockInfoGetter] = None


def on_info(server: PluginServerInterface, info):
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
        if block_info_getter.get_block_info(pei['x'], pei['y'], pei['z']):
            source.reply('§c未能获取方块信息')
            return
        source.reply(f'§a机器处于 §e关闭 §a状态' if block_info_getter.block_data == pei[
            'data'] and block_info_getter.block_name == pei['block'] else '§c机器处于 §e开启 §c状态')
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
    if CP_CONFIG.check_point.get(context['name'], None) is not None:
        source.reply('§c该名字已被使用')
        return
    if block_info_getter.get_block_info(context['x'], context['y'], context['z']):
        source.reply('§c未能获取方块信息')
        return
    jsondata = json.loads(CP_CONFIG.check_point.get(context.get('json', '{}'), '{}'))
    CP_CONFIG.check_point[context['name']] = {'x': context['x'], 'y': context['y'], 'z': context['z'],
                                              'block': block_info_getter.block_name,
                                              'data': jsondata if jsondata != {} else block_info_getter.block_data}
    save_config()
    source.reply('§a添加成功')


def check(source: CommandSource, group=False):
    if group:
        lis = ""
    f = 1
    for index in CP_CONFIG.check_point:
        time.sleep(0.2)
        if block_info_getter.get_block_info(CP_CONFIG.check_point[index]['x'], CP_CONFIG.check_point[index]['y'],
                                            CP_CONFIG.check_point[index]['z']):
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


def help_callback_override(source: CommandSource, context: CommandContext):
    source.reply('§epb已被入注，使用!!pb cp观看入注内容')
    help_callback(source, context)


@new_thread('Pb_CheckPoint_Check')
def make_callback_override(source: CommandSource, context: CommandContext, ignore=True):
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


def on_load(server: PluginServerInterface, prev):
    server.get_plugin_instance()
    global CP_CONFIG, block_info_getter, PlServer
    PlServer = server
    CP_CONFIG = server.load_config_simple(PBCHECKPOINT, target_class=PbCheckPoint, in_data_folder=True)
    block_info_getter = BlockInfoGetter(server)
    pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
    server.get_plugin_command_source()
    try:
        pb: dict = pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._children_literal
    except:
        pass
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

        builder.command(f'{i} add <x> <y> <z> <name>', cmd_add)
        # builder.command(f'{i} add <x> <y> <z> <name> <json>', cmd_add)
        builder.arg('x', Integer)
        builder.arg('y', Integer)
        builder.arg('z', Integer)
        builder.arg('name', GreedyText)
        # builder.arg('json', Text)
        builder.command('ig <comment>', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ignore <comment>', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ig', lambda src, tex: make_callback_override(src, tex, False))
        builder.command('ignore', lambda src, tex: make_callback_override(src, tex, False))
        builder.arg('comment', GreedyText)

    def override_checking(prev):
        global help_callback, make_callback
        def extract_function_name(func_str):
            match = re.match(r"<function (.*?) at", str(func_str))
            if match:
                return match.group(1)
            return None

        while True:
            try:
                timea = time.time()
                i = 1
                while True:
                    if extract_function_name(
                            pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._children_literal['make'][
                                0]._callback) != extract_function_name(make_callback_override):
                        if prev != None and i == 1:
                            i += 1
                            continue
                        make_callback = copy(
                            pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._children_literal['make'][
                                0]._callback)
                        builder.add_children_for(pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node)

                        help_callback = copy(
                            pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._callback)

                        builder.add_children_for(pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node)

                        pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._children_literal['make'][
                            0]._callback = make_callback_override

                        pl.mcdr_server.command_manager.root_nodes['!!pb'][0].node._callback = help_callback_override
                        i += 1
                    if i >= 3 or time.time() - timea >= 2:
                        return
                    time.sleep(0.1)
            except:
                time.sleep(0.1)

    new_thread = threading.Thread(target=override_checking, args=(prev,))
    new_thread.daemon = True  # 设置为守护线程
    new_thread.start()
