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


class PermissionConfig(Serializable):
    """权限配置类"""
    permissions: dict = {
        'list': 1, 'status': 1, 'del': 3, 'update': 2, 'add': 2,
        'add_group': 3, 'add_to_group': 2, 'ignore': 4, 'help': 0, 'helpc': 0
    }


# 权限配置实例
PERM_CONFIG: PermissionConfig = None


def require_permission(perm_key: str):
    """装饰器：使用MCDR内部权限系统检查权限"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(source: CommandSource, context: dict, *args, **kwargs):
            required_level = PERM_CONFIG.permissions.get(perm_key, 1)
            if source.get_permission_level() < required_level:
                source.reply(f'§c权限不足！需要权限等级 {required_level}，当前等级 {source.get_permission_level()}')
                return
            return func(source, context, *args, **kwargs)
        return wrapper
    return decorator


class ParseConfig(Serializable):
    block_info_command: str = 'info block get {x} {y} {z}'
    block_info_regex: re.Pattern = re.compile(r"Block info for (?P<block>minecraft:[\w_]+),")
    block_value_regex: re.Pattern = re.compile(r"(\w+)=([A-Z_]+|\w+)")


class PbCheckPoint(Serializable):
    # 统一的树状结构：既包含检查点元素，也包含分组
    # 格式：{
    #   "name1": {"type": "checkpoint", "x": 1, "y": 2, "z": 3, "world": "overworld", "block": "...", "data": {...}},
    #   "group1": {"type": "group", "description": "分组描述", "children": {...}},
    #   "group1.subgroup": {"type": "group", "description": "子分组", "children": {...}}
    # }
    tree: dict = {}
    override_mode: str = "event"

    # 兼容旧数据的属性
    check_point: dict = {}
    groups: dict = {}


CP_CONFIG: PbCheckPoint


def save_config(path: str = PBCHECKPOINT):
    PlServer.save_config_simple(CP_CONFIG, path)


# ---------- Helper Functions ---------
def get_player_world(source: CommandSource) -> Optional[str]:
    """
    获取玩家所在的世界名称
    返回: 世界名称字符串 (overworld/the_nether/the_end) 或 None
    """
    if not hasattr(source, 'player') or not source.player:
        return None

    try:
        dimension_id = api.get_player_dimension(source.player)
        # 根据 MinecraftDataAPI 文档，维度 ID 映射
        dimension_map = {
            0: 'overworld',
            -1: 'the_nether',
            1: 'the_end'
        }

        # 如果是字符串格式的维度名，直接处理
        if isinstance(dimension_id, str):
            # 处理 minecraft:overworld 格式
            if dimension_id.startswith('minecraft:'):
                dim_name = dimension_id.replace('minecraft:', '')
                if dim_name in ['overworld', 'the_nether', 'the_end']:
                    return dim_name
            return dimension_id.lower()

        # 如果是数字 ID，转换为世界名
        return dimension_map.get(dimension_id, 'overworld')

    except Exception as e:
        PlServer.logger.warning(f'[ExtraPrimeBackup] 获取玩家维度失败: {e}')
        return None


# ---------- InfoManager ---------
class BlockInfoGetter:
    ALLOWED_WORLDS = {"overworld", "the_nether", "the_end"}

    def __init__(self, server: PluginServerInterface):
        self.server: PluginServerInterface = server
        self.block_name: str = ''
        self.block_data: dict = {}
        self.__TIMEOUT = 1
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

@require_permission('help')
def cmd_help(source: CommandSource, context: dict):
    """
    彩色美观、支持what参数、可点击自动填充聊天框的帮助命令
    """
    HELP_DATA = {
        'helpc': {
            'usage': '!!pb cp helpc',
            'desc': '§e📄 输出纯文本指令总览',
            'detail': '输出所有常用子命令及简明中文说明，适合复制、查阅、文档整理。',
            'example': '!!pb cp helpc',
        },
        'list': {
            'usage': '!!pb cp list',
            'desc': '§e📋 列出所有检查点和分组（树状结构）',
            'detail': '列出所有检查点和分组，支持树状结构展示。',
            'example': '!!pb cp list',
        },
        'status': {
            'usage': '!!pb cp status <name>',
            'desc': '§e🔍 查看指定检查点的状态',
            'detail': '显示指定检查点的详细状态，包括坐标、世界、方块类型、属性等。',
            'example': '!!pb cp status factory.redstone.piston',
        },
        'del': {
            'usage': '!!pb cp del <name>',
            'desc': '§e🚮 删除指定检查点或分组',
            'detail': '删除指定检查点或分组（支持嵌套路径）。',
            'example': '!!pb cp del factory.redstone.piston',
        },
        'update': {
            'usage': '!!pb cp update <name>',
            'desc': '§e📁 更新检查点为当前状态',
            'detail': '将指定检查点的方块信息更新为当前位置的状态。',
            'example': '!!pb cp update factory.redstone.piston',
        },
        'add': {
            'usage': '!!pb cp add <x> <y> <z> <name> [world]',
            'desc': '§e➕ 添加新的检查点',
            'detail': '在根级别添加新的检查点，可选world参数自动检测。',
            'example': '!!pb cp add 100 64 200 machine1',
        },
        'add_group': {
            'usage': '!!pb cp add g <group_path>',
            'desc': '§e📁 创建新的分组',
            'detail': '创建新的分组，支持多级嵌套。',
            'example': '!!pb cp add g factory.redstone',
        },
        'add_to_group': {
            'usage': '!!pb cp add g <group_path> <x> <y> <z> <name> [world]',
            'desc': '§e📌 在分组中添加检查点',
            'detail': '在指定分组中添加检查点，支持嵌套路径。',
            'example': '!!pb cp add g factory.redstone 150 64 250 piston',
        },
        'ignore': {
            'usage': '!!pb ignore',
            'desc': '§e🟨 忽略检查点状态强制执行',
            'detail': '强制执行备份操作，忽略所有检查点未关闭的警告。',
            'example': '!!pb ignore',
        },
        'help': {
            'usage': '!!pb cp help [子命令]',
            'desc': '§e❓ 查看帮助',
            'detail': '显示主帮助或指定子命令的详细帮助。',
            'example': '!!pb cp help add',
        },
    }

    # what参数处理
    what = context.get('what')
    if what:
        key = what.lower()
        # 支持别名
        alias_map = {
            'ls': 'list', 'list': 'list',
            'status': 'status', 'st': 'status',
            'del': 'del', 'delete': 'del',
            'update': 'update',
            'add': 'add',
            'addg': 'add_group', 'add_group': 'add_group', 'gr': 'add_group',
            'add_to_group': 'add_to_group',
            'ignore': 'ignore', 'ig': 'ignore',
            'help': 'help',
        }
        key = alias_map.get(key, key)
        if key in HELP_DATA:
            data = HELP_DATA[key]
            # 构建详细帮助RText
            lines = [
                RText(f'§a=== ExtraPrimeBackup 子命令帮助: {key} ==='),
                RText(f'§6用法: ') + RText(data['usage'], RColor.gold).set_click_event(RAction.suggest_command, data['usage']).set_hover_text('§a点击填充到聊天框'),
                RText(f'§6说明: ') + RText(data['desc']),
                RText(f'§6详细: ') + RText(data['detail'], RColor.yellow),
                RText(f'§6示例: ') + RText(data['example'], RColor.aqua).set_click_event(RAction.suggest_command, data['example']).set_hover_text('§a点击填充到聊天框'),
                ]
            for line in lines:
                source.reply(line)
            return
        else:
            source.reply(RText(f'§c未找到子命令 "{what}" 的帮助，可用: ', RColor.red) + RText(', '.join(HELP_DATA.keys()), RColor.yellow))
            return

    # 主帮助列表
    source.reply(RText('§a=== ExtraPrimeBackup 指令帮助 ==='))
    # 分组展示
    group_titles = [
        ('§6检查点管理', ['list', 'status', 'del', 'update', 'add', 'add_group', 'add_to_group']),
        ('§6其他', ['ignore', 'help', 'helpc']),  # 新增 helpc
    ]
    for group_title, cmds in group_titles:
        source.reply(RText(group_title))
        for cmd in cmds:
            data = HELP_DATA[cmd]
            # 主列表每条可点击suggest
            line = RText('  ') + RText(data['desc'], RColor.yellow)
            line.set_click_event(RAction.suggest_command, data['usage'])
            line.set_hover_text(f'§a点击填充: {data["usage"]}\n§7{data["detail"]}')
            source.reply(line)
    # 示例
    source.reply(RText('§6使用示例:'))
    for ex in ['!!pb cp add g factory', '!!pb cp add g factory.redstone', '!!pb cp add 100 64 200 machine1', '!!pb cp add g factory.redstone 150 64 250 piston', '!!pb cp update factory.redstone.piston']:
        source.reply(RText('  ') + RText(ex, RColor.aqua).set_click_event(RAction.suggest_command, ex).set_hover_text('§a点击填充到聊天框'))
    source.reply(RText('§7输入 §e!!pb cp help <子命令> §7可查看详细用法'))
    return


@require_permission('helpc')
def cmd_helpc(source: CommandSource, context: dict):
    """
    输出所有子命令及说明，全部为简明中文纯文本，便于复制
    """
    source.reply('=== ExtraPrimeBackup 指令总览 ===')
    source.reply('本命令用于输出所有常用子命令及简明中文说明，适合复制、查阅、文档整理。')
    source.reply('如需详细用法请用 !!pb cp help <子命令>，如 !!pb cp help add')
    HELP_LIST = [
        ('!!pb cp list', '列出所有检查点和分组（树状结构）'),
        ('!!pb cp status <name>', '查看指定检查点的状态'),
        ('!!pb cp del <name>', '删除指定检查点或分组'),
        ('!!pb cp update <name>', '更新检查点为当前状态'),
        ('!!pb cp add <x> <y> <z> <name> [world]', '添加新的检查点'),
        ('!!pb cp add g <group_path>', '创建新的分组（支持嵌套）'),
        ('!!pb cp add g <group_path> <x> <y> <z> <name> [world]', '在指定分组中添加检查点'),
        ('!!pb ignore', '忽略检查点状态强制执行'),
        ('!!pb cp help [子命令]', '查看帮助'),
        ('!!pb cp helpc', '输出本列表（纯文本总览）'),
    ]
    for cmd, desc in HELP_LIST:
        source.reply(f'{cmd}    {desc}')
    return

# 应用权限装饰器到所有命令函数
@require_permission('list')
@new_thread('Pb_CheckPoint_List')
def cmd_list(source: CommandSource, context: dict):
    """列出检查点，支持树状结构显示"""

    def display_tree(tree_dict, indent=0, path_prefix=""):
        """递归显示树状结构"""
        prefix = "  " * indent
        for name, item in tree_dict.items():
            if item['type'] == 'group':
                # 分组显示为红色
                desc = f" - {item.get('description', '')}" if item.get('description') else ""
                source.reply(f'{prefix}§c📁 {name}{desc}')
                # 递归显示子项
                children = item.get('children', {})
                if children:
                    new_path = f"{path_prefix}.{name}" if path_prefix else name
                    display_tree(children, indent + 1, new_path)
            elif item['type'] == 'checkpoint':
                # 检查点显示为黄色，添加可点击功能
                world = item.get('world', 'overworld')
                x, y, z = item.get('x', 0), item.get('y', 0), item.get('z', 0)

                # 构建完整路径用于命令
                full_path = f"{path_prefix}.{name}" if path_prefix else name

                # 创建可点击的 RText
                checkpoint_text = RText(f'{prefix}§e📌 {name} §7({x}, {y}, {z}) in {world}')
                checkpoint_text.set_hover_text('§a点击查看详情')
                checkpoint_text.set_click_event(RAction.run_command, f'!!pb cp status {full_path}')

                source.reply(checkpoint_text)

    if not CP_CONFIG.tree:
        # 如果新结构为空，检查旧数据
        if CP_CONFIG.check_point:
            source.reply('§e=== 检查点列表（旧格式） ===')
            for name, info in CP_CONFIG.check_point.items():
                world = info.get('world', 'overworld')
                x, y, z = info.get('x', 0), info.get('y', 0), info.get('z', 0)

                # 旧格式也添加可点击功能
                checkpoint_text = RText(f'§e{name} §7({x}, {y}, {z}) in {world}')
                checkpoint_text.set_hover_text('§a点击查看详情')
                checkpoint_text.set_click_event(RAction.run_command, f'!!pb cp status {name}')

                source.reply(checkpoint_text)
        else:
            source.reply('§e没有任何检查点')
        return

    source.reply('§a=== 检查点树状结构 ===')
    display_tree(CP_CONFIG.tree)


@require_permission('status')
@new_thread('Pb_CheckPoint_Status')
def cmd_status(source: CommandSource, context: dict):
    """显示检查点状态，支持新树状结构和嵌套路径，以树状格式显示详细信息"""
    item_name = context.get('name') or context.get('n')

    def find_in_tree(tree_dict, path_parts):
        """递归查找树状结构中的检查点"""
        if len(path_parts) == 1:
            name = path_parts[0]
            if name in tree_dict and tree_dict[name]['type'] == 'checkpoint':
                return tree_dict[name]
            return None
        else:
            parent = path_parts[0]
            if parent in tree_dict and tree_dict[parent]['type'] == 'group':
                children = tree_dict[parent].get('children', {})
                return find_in_tree(children, path_parts[1:])
            return None

    def display_status_tree(checkpoint_data, actual_block, actual_data, success):
        """以树状格式显示检查点状态信息"""
        source.reply(f'§a=== 检查点状态：{item_name} ===')

        # 基本信息
        source.reply('§6├─ 基本信息')
        source.reply(f'§7│  ├─ 坐标: §e({checkpoint_data["x"]}, {checkpoint_data["y"]}, {checkpoint_data["z"]})')
        source.reply(f'§7│  ├─ 世界: §e{checkpoint_data.get("world", "overworld")}')
        source.reply(f'§7│  └─ 获取状态: {"§a成功" if success else "§c失败"}')

        # 配置中的方块信息
        source.reply('§6├─ 配置数据')
        source.reply(f'§7│  ├─ 方块类型: §e{checkpoint_data.get("block", "未知")}')
        config_data = checkpoint_data.get("data", {})
        if config_data:
            source.reply('§7│  └─ 方块属性:')
            data_items = list(config_data.items())
            for i, (key, value) in enumerate(data_items):
                is_last = (i == len(data_items) - 1)
                branch = "└─" if is_last else "├─"
                source.reply(f'§7│     {branch} §b{key}§7: §e{value}')
        else:
            source.reply('§7│  └─ 方块属性: §8无')

        if success:
            # 实际获取的方块信息
            source.reply('§6├─ 实际数据')
            source.reply(f'§7│  ├─ 方块类型: §e{actual_block}')
            if actual_data:
                source.reply('§7│  └─ 方块属性:')
                actual_items = list(actual_data.items())
                for i, (key, value) in enumerate(actual_items):
                    is_last = (i == len(actual_items) - 1)
                    branch = "└─" if is_last else "├─"
                    source.reply(f'§7│     {branch} §b{key}§7: §e{value}')
            else:
                source.reply('§7│  └─ 方块属性: §8无')

            # 对比结果
            block_match = (actual_block == checkpoint_data.get("block", ""))
            data_match = (actual_data == config_data)
            overall_match = block_match and data_match

            source.reply('§6├─ 状态分析')
            source.reply(f'§7│  ├─ 方块类型匹配: {"§a是" if block_match else "§c否"}')
            source.reply(f'§7│  ├─ 方块属性匹配: {"§a是" if data_match else "§c否"}')
            source.reply(f'§7│  └─ 整体状态: {"§a机器已关闭" if overall_match else "§c机器正在运行"}')
        else:
            source.reply('§6├─ §c无法获取实际数据进行对比')

        # 操作按钮
        source.reply('§6└─ 操作选项')

        # 删除按钮
        delete_btn = RText('§c[删除]')
        delete_btn.set_hover_text('§c点击删除此检查点')
        delete_btn.set_click_event(RAction.run_command, f'!!pb cp del {item_name}')

        # 更新按钮
        update_btn = RText('§e[更新]')
        update_btn.set_hover_text('§e点击更新此检查点为当前状态')
        update_btn.set_click_event(RAction.run_command, f'!!pb cp update {item_name}')

        # 显示按钮行 - 使用 + 操作符组合 RText
        button_line = RText('§7   ') + delete_btn + RText('§7 ') + update_btn

        source.reply(button_line)

    # 支持嵌套路径查找
    path_parts = item_name.split('.')
    checkpoint = find_in_tree(CP_CONFIG.tree, path_parts)

    if checkpoint:
        world = checkpoint.get('world', 'overworld')
        success = not block_info_getter.get_block_info(checkpoint['x'], checkpoint['y'], checkpoint['z'], world)

        display_status_tree(
            checkpoint,
            block_info_getter.block_name if success else "获取失败",
            block_info_getter.block_data if success else {},
            success
        )
    else:
        # 兼容旧数据
        if item_name in CP_CONFIG.check_point:
            pei = CP_CONFIG.check_point[item_name]
            world = pei.get('world', 'overworld')  # 兼容旧数据，默认overworld
            success = not block_info_getter.get_block_info(pei['x'], pei['y'], pei['z'], world)

            display_status_tree(
                pei,
                block_info_getter.block_name if success else "获取失败",
                block_info_getter.block_data if success else {},
                success
            )
        else:
            source.reply('§c配置不存在')


@require_permission('del')
@new_thread('Pb_CheckPoint_Del')
def cmd_del(source: CommandSource, context: dict):
    """删除检查点或分组"""
    item_name = context.get('name') or context.get('n')

    def delete_from_tree(tree_dict, path_parts):
        """递归删除树状结构中的项目"""
        if len(path_parts) == 1:
            name = path_parts[0]
            if name in tree_dict:
                del tree_dict[name]
                return True
            return False
        else:
            parent = path_parts[0]
            if parent in tree_dict and tree_dict[parent]['type'] == 'group':
                children = tree_dict[parent].get('children', {})
                return delete_from_tree(children, path_parts[1:])
            return False

    # 支持删除嵌套路径
    path_parts = item_name.split('.')

    if delete_from_tree(CP_CONFIG.tree, path_parts):
        save_config()
        source.reply(f'§a删除成功：{item_name}')
    else:
        # 兼容旧数据
        if item_name in CP_CONFIG.check_point:
            del CP_CONFIG.check_point[item_name]
            # 从所有分组中移除
            for group_name, group_data in CP_CONFIG.groups.items():
                if item_name in group_data.get('items', []):
                    group_data['items'].remove(item_name)
            save_config()
            source.reply(f'§a删除成功：{item_name}')
        else:
            source.reply('§e配置不存在')


@require_permission('add')
@new_thread('Pb_CheckPoint_Add')
def cmd_add(source: CommandSource, context: dict):
    # 解析路径和名称
    name = context.get('name') or context.get('n')
    path_parts = name.split('.')

    # 如果只有坐标参数，直接添加到根级别
    if len(path_parts) == 1:
        # 检查名字是否已存在
        if name in CP_CONFIG.tree:
            source.reply('§c该名字已被使用')
            return

        # 获取坐标信息
        x, y, z = context['x'], context['y'], context['z']
        world = context.get('world')

        # world参数处理
        if not world:
            world = get_player_world(source)
            if not world:
                source.reply('§c无法自动获取玩家维度，请手动指定 world (overworld/the_nether/the_end)')
                return
        world = str(world).lower()
        if world not in BlockInfoGetter.ALLOWED_WORLDS:
            source.reply('§cworld参数非法，仅支持 overworld/the_nether/the_end')
            return

        # 获取方块信息
        if block_info_getter.get_block_info(x, y, z, world):
            source.reply('§c未能获取方块信息')
            return

        # 添加检查点到树状结构
        CP_CONFIG.tree[name] = {
            'type': 'checkpoint',
            'x': x,
            'y': y,
            'z': z,
            'world': world,
            'block': block_info_getter.block_name,
            'data': block_info_getter.block_data
        }
        save_config()
        source.reply(f'§a成功添加检查点 "{name}"')

    else:
        # 有路径，表示要添加到指定分组
        if len(path_parts) < 2:
            source.reply('§c路径格式错误，应为：group.subgroup.name')
            return

        group_path = '.'.join(path_parts[:-1])
        item_name = path_parts[-1]

        # 检查分组是否存在
        current = CP_CONFIG.tree
        for part in group_path.split('.'):
            if part not in current:
                source.reply(f'§c分组路径 "{group_path}" 不存在，请先创建分组')
                return
            if current[part]['type'] != 'group':
                source.reply(f'§c路径 "{part}" 不是分组')
                return
            current = current[part].setdefault('children', {})

        # 检查名字是否已在该分组中存在
        if item_name in current:
            source.reply(f'§c名字 "{item_name}" 在分组 "{group_path}" 中已存在')
            return

        # 获取坐标信息
        x, y, z = context['x'], context['y'], context['z']
        world = context.get('world')

        # world参数处理
        if not world:
            world = get_player_world(source)
            if not world:
                source.reply('§c无法自动获取玩家维度，请手动指定 world (overworld/the_nether/the_end)')
                return
        world = str(world).lower()
        if world not in BlockInfoGetter.ALLOWED_WORLDS:
            source.reply('§cworld参数非法，仅支持 overworld/the_nether/the_end')
            return

        # 获取方块信息
        if block_info_getter.get_block_info(x, y, z, world):
            source.reply('§c未能获取方块信息')
            return

        # 添加检查点到指定分组
        current[item_name] = {
            'type': 'checkpoint',
            'x': x,
            'y': y,
            'z': z,
            'world': world,
            'block': block_info_getter.block_name,
            'data': block_info_getter.block_data
        }
        save_config()
        source.reply(f'§a成功在分组 "{group_path}" 中添加检查点 "{item_name}"')


@require_permission('add_group')
@new_thread('Pb_CheckPoint_AddG')
def cmd_add_group(source: CommandSource, context: dict):
    """添加分组，支持多级嵌套路径"""
    group_path = context['group_path']

    if not group_path:
        source.reply('§c分组名不能为空')
        return

    # 解析路径
    path_parts = group_path.split('.')
    current = CP_CONFIG.tree

    # 检查并创建路径
    for i, part in enumerate(path_parts):
        if part in current:
            if current[part]['type'] != 'group':
                current_path = '.'.join(path_parts[:i + 1])
                source.reply(f'§c路径 "{current_path}" 已存在且不是分组')
                return
            current = current[part].setdefault('children', {})
        else:
            # 创建新分组
            current[part] = {
                'type': 'group',
                'description': '',
                'children': {}
            }
            if i < len(path_parts) - 1:
                current = current[part]['children']

    save_config()
    source.reply(f'§a成功创建分组 "{group_path}"')


def check(source: CommandSource, group=False):
    """检查所有检查点状态，支持新树状结构和旧数据兼容"""
    if group:
        lis = ""
    f = 1

    def check_tree_checkpoints(tree_dict, path_prefix=""):
        """递归检查树状结构中的所有检查点"""
        nonlocal lis, f
        for name, item in tree_dict.items():
            if item['type'] == 'checkpoint':
                full_name = f"{path_prefix}.{name}" if path_prefix else name
                time.sleep(0.2)
                world = item.get('world', 'overworld')
                if block_info_getter.get_block_info(item['x'], item['y'], item['z'], world):
                    if not group:
                        source.reply(f'§c未能获取机器 §e{full_name} 的状态')
                    f = 0
                    continue
                if block_info_getter.block_name != item['block'] or block_info_getter.block_data != item['data']:
                    if group:
                        lis += full_name + ','
                    if not group:
                        source.get_server().broadcast(f'§c机器 §e{full_name} §c貌似没有关闭')
                    f = 0
            elif item['type'] == 'group':
                children = item.get('children', {})
                if children:
                    new_prefix = f"{path_prefix}.{name}" if path_prefix else name
                    check_tree_checkpoints(children, new_prefix)

    # 检查新树状结构
    if CP_CONFIG.tree:
        check_tree_checkpoints(CP_CONFIG.tree)

    # 兼容检查旧数据
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


@require_permission('ignore')
@new_thread('Pb_CheckPoint_Make')
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
    global CP_CONFIG, block_info_getter, PlServer, override_monitor_thread, override_monitor_running, PERM_CONFIG
    block_info_getter = BlockInfoGetter(server)
    PlServer = server

    # 使用MCDR标准方法加载权限配置
    PERM_CONFIG = server.load_config_simple('config.json', target_class=PermissionConfig)

    # 加载检查点配置
    CP_CONFIG = server.load_config_simple(PBCHECKPOINT, target_class=PbCheckPoint, in_data_folder=True)
    override_mode = CP_CONFIG.override_mode
    pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
    server.get_plugin_command_source()
    builder = SimpleCommandBuilder()
    for i in ['cp', 'checkpoint']:
        builder.command(i, cmd_help)
        builder.command(f'{i} help', cmd_help)
        builder.command(f'{i} help <what>', cmd_help)
        builder.command(f'{i} helpc', cmd_helpc)  # 注册helpc指令
        builder.arg('what', Text)

        # 检查点管理
        builder.command(f'{i} list', cmd_list)
        builder.command(f'{i} list tree', lambda src, ctx: cmd_list(src, {**ctx, 'tree': True}))
        builder.command(f'{i} ls', cmd_list)
        builder.command(f'{i} status <name>', cmd_status)
        builder.command(f'{i} st <name>', cmd_status)
        builder.command(f'{i} del <name>', cmd_del)
        builder.command(f'{i} update <name>', cmd_update)
        # 添加分组
        builder.command(f'{i} add g <group_path>', cmd_add_group)
        # 添加检查点到指定分组
        builder.command(f'{i} add g <group_path> <x> <y> <z> <name>', cmd_add_to_group)
        builder.command(f'{i} add g <group_path> <x> <y> <z> <name> <world>', cmd_add_to_group)

        builder.command(f'{i} add gr <group_path>', cmd_add_group)
        builder.command(f'{i} add gr <group_path> <x> <y> <z> <name>', cmd_add_to_group)
        builder.command(f'{i} add gr <group_path> <x> <y> <z> <name> <world>', cmd_add_to_group)

        builder.command(f'{i} add <x> <y> <z> <name>', cmd_add)
        builder.command(f'{i} add <x> <y> <z> <name> <world>', cmd_add)

        # 参数定义
        builder.arg('x', Integer)
        builder.arg('y', Integer)
        builder.arg('z', Integer)
        builder.arg('n', Text)  # 统一使用 n 作为参数名
        builder.arg('name', Text)  # 保留 name 以兼容
        builder.arg('world', Text)
        builder.arg('group_path', Text)

        # 忽略命令
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
    插件卸载时优雅地停止监控线程、取消覆写、清除命令并重载 PrimeBackup 插件
    """
    global override_monitor_running, override_monitor_thread, help_callback, make_callback

    # 1. 停止监控线程
    with override_monitor_lock:
        if override_monitor_thread is not None and override_monitor_thread.is_alive():
            server.logger.info('[ExtraPrimeBackup] 正在停止覆写监控线程...')
            override_monitor_running = False
            override_monitor_thread.join(timeout=2)
            if override_monitor_thread.is_alive():
                server.logger.warning('[ExtraPrimeBackup] 监控线程未能在超时时间内停止')
            else:
                server.logger.info('[ExtraPrimeBackup] 监控线程已成功停止')

    # 2. 取消覆写，恢复原始回调函数
    try:
        pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
        node = pl.mcdr_server.command_manager.root_nodes.get('!!pb', [None])[0]
        if node is not None:
            make_node = node.node._children_literal.get('make', [None])[0]

            # 恢复原始的 make 回调函数
            if make_node is not None and make_callback is not None:
                make_node._callback = make_callback
                server.logger.info('[ExtraPrimeBackup] 已恢复原始 make 回调函数')

            # 恢复原始的 help 回调函数
            if help_callback is not None:
                node.node._callback = help_callback
                server.logger.info('[ExtraPrimeBackup] 已恢复原始 help 回调函数')

            server.logger.info('[ExtraPrimeBackup] 取消覆写成功，已恢复 PrimeBackup 原始功能')
    except Exception as e:
        server.logger.warning(f'[ExtraPrimeBackup] 取消覆写时发生异常: {e}')

    # 3. 清除我们添加的命令（cp、checkpoint、ignore等）
    try:
        pl: AbstractPlugin = getattr(server, '_PluginServerInterface__plugin')
        node = pl.mcdr_server.command_manager.root_nodes.get('!!pb', [None])[0]
        if node is not None:
            # 清除 cp 和 checkpoint 命令
            commands_to_remove = ['cp', 'checkpoint', 'ig', 'ignore']
            for cmd in commands_to_remove:
                if cmd in node.node._children_literal:
                    del node.node._children_literal[cmd]
                    server.logger.info(f'[ExtraPrimeBackup] 已清除命令: !!pb {cmd}')

            server.logger.info('[ExtraPrimeBackup] 成功清除所有添加的命令')
    except Exception as e:
        server.logger.warning(f'[ExtraPrimeBackup] 清除命令时发生异常: {e}')

    # 5. 清理全局变量
    help_callback = None
    make_callback = None

    server.logger.info('[ExtraPrimeBackup] 插件完全卸载完成，所有命令已清除')


@require_permission('add_to_group')
@new_thread('Pb_CheckPoint_AddGT')
def cmd_add_to_group(source: CommandSource, context: dict):
    """向指定分组添加检查点"""
    group_path = context['group_path']
    name = context.get('name') or context.get('n')
    x, y, z = context['x'], context['y'], context['z']
    world = context.get('world')

    # world参数处理
    if not world:
        world = get_player_world(source)
        if not world:
            source.reply('§c无法自动获取玩家维度，请手动指定 world (overworld/the_nether/the_end)')
            return
    world = str(world).lower()
    if world not in BlockInfoGetter.ALLOWED_WORLDS:
        source.reply('§cworld参数非法，仅支持 overworld/the_nether/the_end')
        return

    # 检查分组是否存在
    current = CP_CONFIG.tree
    path_parts = group_path.split('.')
    for part in path_parts:
        if part not in current:
            source.reply(f'§c分组路径 "{group_path}" 不存在，请先创建分组')
            return
        if current[part]['type'] != 'group':
            source.reply(f'§c路径 "{part}" 不是分组')
            return
        current = current[part].setdefault('children', {})

    # 检查名字是否已在该分组中存在
    if name in current:
        source.reply(f'§c名字 "{name}" 在分组 "{group_path}" 中已存在')
        return

    # 获取方块信息
    if block_info_getter.get_block_info(x, y, z, world):
        source.reply('§c未能获取方块信息')
        return

    # 添加检查点到指定分组
    current[name] = {
        'type': 'checkpoint',
        'x': x,
        'y': y,
        'z': z,
        'world': world,
        'block': block_info_getter.block_name,
        'data': block_info_getter.block_data
    }
    save_config()
    source.reply(f'§a成功在分组 "{group_path}" 中添加检查点 "{name}"')


@require_permission('update')
@new_thread('Pb_CheckPoint_Update')
def cmd_update(source: CommandSource, context: dict):
    """更新检查点：先删除后重新创建"""
    item_name = context.get('name') or context.get('n')

    def find_in_tree(tree_dict, path_parts):
        """递归查找树状结构中的检查点"""
        if len(path_parts) == 1:
            name = path_parts[0]
            if name in tree_dict and tree_dict[name]['type'] == 'checkpoint':
                return tree_dict[name]
            return None
        else:
            parent = path_parts[0]
            if parent in tree_dict and tree_dict[parent]['type'] == 'group':
                children = tree_dict[parent].get('children', {})
                return find_in_tree(children, path_parts[1:])
            return None

    def delete_from_tree(tree_dict, path_parts):
        """递归删除树状结构中的项目"""
        if len(path_parts) == 1:
            name = path_parts[0]
            if name in tree_dict:
                del tree_dict[name]
                return True
            return False
        else:
            parent = path_parts[0]
            if parent in tree_dict and tree_dict[parent]['type'] == 'group':
                children = tree_dict[parent].get('children', {})
                return delete_from_tree(children, path_parts[1:])
            return False

    def add_to_tree(tree_dict, path_parts, checkpoint_data):
        """递归添加检查点到树状结构"""
        if len(path_parts) == 1:
            name = path_parts[0]
            tree_dict[name] = checkpoint_data
            return True
        else:
            parent = path_parts[0]
            if parent in tree_dict and tree_dict[parent]['type'] == 'group':
                children = tree_dict[parent].get('children', {})
                return add_to_tree(children, path_parts[1:], checkpoint_data)
            return False

    # 支持嵌套路径
    path_parts = item_name.split('.')

    # 首先查找现有检查点
    checkpoint = find_in_tree(CP_CONFIG.tree, path_parts)
    if not checkpoint and item_name not in CP_CONFIG.check_point:
        source.reply('§c检查点不存在')
        return

    # 获取坐标信息（从现有检查点或旧数据）
    if checkpoint:
        x, y, z = checkpoint['x'], checkpoint['y'], checkpoint['z']
        world = checkpoint.get('world', 'overworld')
    else:
        # 兼容旧数据
        pei = CP_CONFIG.check_point[item_name]
        x, y, z = pei['x'], pei['y'], pei['z']
        world = pei.get('world', 'overworld')

    # 获取当前方块信息
    if block_info_getter.get_block_info(x, y, z, world):
        source.reply('§c未能获取方块信息，更新失败')
        return

    # 删除旧的检查点
    deleted_from_tree = delete_from_tree(CP_CONFIG.tree, path_parts)
    if not deleted_from_tree and item_name in CP_CONFIG.check_point:
        del CP_CONFIG.check_point[item_name]
        # 从所有分组中移除
        for group_name, group_data in CP_CONFIG.groups.items():
            if item_name in group_data.get('items', []):
                group_data['items'].remove(item_name)

    # 创建新的检查点数据
    new_checkpoint = {
        'type': 'checkpoint',
        'x': x,
        'y': y,
        'z': z,
        'world': world,
        'block': block_info_getter.block_name,
        'data': block_info_getter.block_data
    }

    # 添加回树状结构（如果原来在树中）
    if deleted_from_tree:
        add_to_tree(CP_CONFIG.tree, path_parts, new_checkpoint)
    else:
        # 如果是旧数据，添加到根级别
        CP_CONFIG.tree[item_name] = new_checkpoint

    save_config()
    source.reply(f'§a成功更新检查点 "{item_name}" 为当前状态')
