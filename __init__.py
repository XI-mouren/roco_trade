from nonebot import get_plugin_config, get_driver, on_command, on_message, get_bot
from nonebot.plugin import PluginMetadata
from nonebot.params import CommandArg
from nonebot.adapters import Event, Message
from nonebot.rule import Rule
from nonebot_plugin_apscheduler import scheduler
from datetime import datetime, timedelta
from typing import Dict

from .config import Config
from .database import Database
from .ocr_recognizer import OCRRecognizer
from .price_parser import PriceParser

__plugin_meta__ = PluginMetadata(
    name="倒货助手",
    description="游戏市场倒货收益计算助手",
    usage="/记录 角色名 市场名 + 图片\n/我的市场\n/查价\n/历史低价\n/倒货帮助",
    config=Config,
)

config = get_plugin_config(Config)

db = Database(config.db_path, config.day_boundary_hour)
ocr = OCRRecognizer(config.ocr_lang)
parser = PriceParser(ocr, config.markets)

user_temp: Dict[str, Dict] = {}
record_sessions: Dict[str, Dict] = {}
TIMER_SECONDS = 30

driver = get_driver()

@driver.on_startup
async def init_db():
    await db.connect()

record_cmd = on_command("记录", aliases={"录入"}, block=True, priority=10)
my_cmd = on_command("我的市场", block=True, priority=10)
check_cmd = on_command("查价", aliases={"查看收益"}, block=True, priority=10)
history_cmd = on_command("历史低价", block=True, priority=10)
help_cmd = on_command("倒货帮助", aliases={"帮助"}, block=True, priority=10)


def _is_collecting(event: Event) -> bool:
    uid = event.get_user_id()
    if uid not in record_sessions:
        return False
    return any(s.type == "image" for s in event.message)


collector = on_message(rule=Rule(_is_collecting), priority=5, block=True)


@collector.handle()
async def collect_images(event: Event):
    uid = event.get_user_id()
    imgs = [s.data["url"] for s in event.message if s.type == "image"]
    record_sessions[uid]["images"].extend(imgs)
    await collector.finish(f"已接收 {len(record_sessions[uid]['images'])} 张截图")


@record_cmd.handle()
async def handle_record(event: Event, args: Message = CommandArg()):
    user_id = event.get_user_id()
    text = args.extract_plain_text().strip()

    if not text:
        await record_cmd.finish("格式：/记录 角色名 市场名\n或：/记录 角色名（一次性上传3个市场）")

    parts = text.split()
    if len(parts) >= 2:
        world, market = parts[0], parts[1]
    elif len(parts) == 1:
        world, market = parts[0], None
    else:
        await record_cmd.finish("格式：/记录 角色名 市场名")

    record_sessions[user_id] = {
        "world": world,
        "market": market,
        "images": [],
        "event": event,
    }

    images = [seg.data["url"] for seg in event.message if seg.type == "image"]
    if images:
        record_sessions[user_id]["images"].extend(images)

    if scheduler.get_job(f"record_{user_id}"):
        scheduler.remove_job(f"record_{user_id}")
    run_date = datetime.now() + timedelta(seconds=TIMER_SECONDS)
    scheduler.add_job(
        process_session,
        "date",
        run_date=run_date,
        id=f"record_{user_id}",
        args=[user_id],
    )

    prompt = (
        f"已开启 {world}{' '+market if market else ''} 录入窗口（{TIMER_SECONDS}秒），请发送截图"
        + f"，已收到 {len(record_sessions[user_id]['images'])} 张"
        if record_sessions[user_id]["images"]
        else f"已开启 {world}{' '+market if market else ''} 录入窗口（{TIMER_SECONDS}秒），请发送截图"
    )
    await record_cmd.finish(prompt)


async def process_session(user_id: str):
    session = record_sessions.pop(user_id, None)
    if not session:
        return
    bot = get_bot()
    event = session["event"]
    world = session["world"]
    market = session["market"]
    received = session["images"]

    if not received:
        await bot.send(event=event, message=f"窗口结束但未收到 {world} 的截图")
        return

    if market:
        try:
            image_data = await download_image(received[0])
            market_name, products = parser.parse_screenshot(image_data)
            if market_name == "未知市场":
                market_name = market

            existing = await db.get_existing_products(user_id, world, market_name)
            all_products, added, skipped = parser.merge_products(existing, products)

            if skipped:
                skip_names = [f"{p['name']}({p['price']})" for p in skipped]
                await bot.send(event=event, message=f"以下商品已存在：{', '.join(skip_names)}")

            await db.upsert_daily_price(user_id, world, market_name, all_products)
            await db.calculate_yields(user_id, world)

            historical_lows = []
            for product in products:
                cursor = await db.db.execute(
                    "SELECT same_world_profit FROM daily_yield WHERE user_id=? AND world=? AND date=? AND product_name=?",
                    (user_id, world, db.get_current_date(), product["name"])
                )
                row = await cursor.fetchone()
                if row and row[0] is not None:
                    is_low = await db.check_historical_low(product["name"], row[0], user_id, world)
                    if is_low:
                        historical_lows.append(f"{product['name']}({row[0]}元)")

            if added:
                await bot.send(event=event, message=f"已记录 {market_name}：{', '.join(added)}")
            else:
                await bot.send(event=event, message=f"{market_name} 无新增商品")

            if historical_lows:
                await bot.send(event=event, message=f"历史新低：{', '.join(historical_lows)}")
        except Exception as e:
            await bot.send(event=event, message=f"录入出错：{e}")
    else:
        if len(received) < 3:
            await bot.send(event=event, message=f"还需要 {3 - len(received)} 张截图（共需3张）")
            return

        try:
            temp_key = f"{user_id}_{world}"
            user_temp[temp_key] = {"world": world, "markets": {}}

            for img_url in received[:3]:
                image_data = await download_image(img_url)
                market_name, products = parser.parse_screenshot(image_data)
                user_temp[temp_key]["markets"][market_name] = products

            for market_name, products in user_temp[temp_key]["markets"].items():
                existing = await db.get_existing_products(user_id, world, market_name)
                all_products, added, skipped = parser.merge_products(existing, products)
                await db.upsert_daily_price(user_id, world, market_name, all_products)
                await db.calculate_yields(user_id, world)

            del user_temp[temp_key]
            await bot.send(event=event, message=f"已记录 {world} 的3个市场数据")
        except Exception as e:
            await bot.send(event=event, message=f"录入出错：{e}")


@my_cmd.handle()
async def handle_my(event: Event):
    user_id = event.get_user_id()
    records = await db.get_user_records(user_id)

    if not records:
        await my_cmd.finish("今日暂无记录")

    known_markets = set(config.markets)
    msg = "今日记录：\n"
    for world, markets in records.items():
        msg += f"\n【{world}】\n"
        uploaded_markets = set()
        for market, products in markets.items():
            product_list = [f"  {p['name']}({p['price']})" for p in products]
            msg += f"  {market}：\n" + "\n".join(product_list) + "\n\n"
            uploaded_markets.add(market)
        missing = known_markets - uploaded_markets
        if missing:
            msg += f"  ⚠️ 未上传：{', '.join(missing)}\n"

    await my_cmd.finish(msg.strip())


@check_cmd.handle()
async def handle_check(event: Event):
    user_id = event.get_user_id()
    best_yields = await db.get_best_yields()

    if not best_yields:
        await check_cmd.finish("今日暂无收益数据")

    msg = "今日最佳收益：\n"
    for product, data in best_yields.items():
        msg += f"\n【{product}】\n"
        same = data["same_world"]
        if same["profit"] is not None:
            msg += f"  同世界：{same['profit']}元（用户{same['user_id']}的{same['world']}）\n"
        cross = data["cross_world"]
        if cross["profit"] is not None:
            msg += f"  跨世界：{cross['profit']}元\n"
            msg += f"    买入：用户{cross['buy_user']}的{cross['buy_world']}{cross['buy_market']}\n"
            msg += f"    卖出：用户{cross['sell_user']}的{cross['sell_world']}{cross['sell_market']}\n"

    historical_lows = await db.get_historical_lows()
    if historical_lows:
        msg += "\n历史最低：\n"
        for hl in historical_lows[:5]:
            msg += f"  {hl['product']}：{hl['yield']}元(用户{hl['user_id']}的{hl['world']})\n"

    await check_cmd.finish(msg.strip())


@history_cmd.handle()
async def handle_history(event: Event):
    historical_lows = await db.get_historical_lows()

    if not historical_lows:
        await history_cmd.finish("暂无历史低价记录")

    msg = "历史最低记录：\n"
    for hl in historical_lows:
        msg += f"\n{hl['product']}：{hl['yield']}元\n"
        msg += f"  用户：{hl['user_id']}\n"
        msg += f"  世界：{hl['world']}\n"
        msg += f"  日期：{hl['date']}\n"

    await history_cmd.finish(msg.strip())


@help_cmd.handle()
async def handle_help(event: Event):
    msg = """【倒货助手使用指南】

【第一步：上传截图】
格式：/记录 角色名 市场名 + 图片
示例：/记录 主号 潮汐集市1号摊

支持一次性上传3个市场：
格式：/记录 角色名 + 3张图片

【第二步：查看记录】
发送 /我的市场 查看今日已上传的所有商品
根据提示补充缺失的市场

【第三步：查看收益】
发送 /查价 查看当日各商品最佳收益排行
- 同世界最佳：同一角色内不同市场间的价差
- 跨世界最佳：不同用户角色间的买卖差价

【第四步：历史低价】
发送 /历史低价 查看历史新低商品

【注意事项】
1. 日期以凌晨4点为界，4点前上传的数据属于前一天
2. 同一市场重复上传时，已有商品会提示是否覆盖
3. 收益只基于已上传的市场计算，请确保数据完整
4. 收益 = 最高价 - 最低价"""

    await help_cmd.finish(msg)


async def download_image(url: str) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
            raise Exception(f"下载图片失败：{resp.status}")
