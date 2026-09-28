import aiosqlite
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple


class Database:
    def __init__(self, db_path: str, day_boundary_hour: int = 4):
        self.db_path = db_path
        self.day_boundary_hour = day_boundary_hour
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()

    async def close(self):
        if self.db:
            await self.db.close()

    async def _create_tables(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS daily_price (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                world TEXT NOT NULL,
                date TEXT NOT NULL,
                market TEXT NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, world, date, market, product_name)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS daily_yield (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                world TEXT NOT NULL,
                date TEXT NOT NULL,
                product_name TEXT NOT NULL,
                same_world_profit REAL,
                cross_world_profit REAL,
                buy_user TEXT,
                buy_world TEXT,
                buy_market TEXT,
                sell_user TEXT,
                sell_world TEXT,
                sell_market TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, world, date, product_name)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS historical_low (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT NOT NULL UNIQUE,
                low_yield REAL NOT NULL,
                user_id TEXT NOT NULL,
                world TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.commit()

    def get_current_date(self) -> str:
        now = datetime.now()
        if now.hour < self.day_boundary_hour:
            now = now - timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    async def upsert_daily_price(self, user_id: str, world: str, market: str, products: List[Dict]) -> List[str]:
        date = self.get_current_date()
        new_products = []
        for product in products:
            name = product["name"]
            price = product["price"]
            cursor = await self.db.execute(
                "SELECT id, price FROM daily_price WHERE user_id=? AND world=? AND date=? AND market=? AND product_name=?",
                (user_id, world, date, market, name)
            )
            existing = await cursor.fetchone()
            if existing:
                if existing[1] != price:
                    new_products.append(f"{name}(已更新: {existing[1]}->{price})")
                    await self.db.execute(
                        "UPDATE daily_price SET price=? WHERE id=?",
                        (price, existing[0])
                    )
            else:
                new_products.append(f"{name}({price})")
                await self.db.execute(
                    "INSERT INTO daily_price (user_id, world, date, market, product_name, price) VALUES (?,?,?,?,?,?)",
                    (user_id, world, date, market, name, price)
                )
        await self.db.commit()
        return new_products

    async def get_existing_products(self, user_id: str, world: str, market: str) -> List[Dict]:
        date = self.get_current_date()
        cursor = await self.db.execute(
            "SELECT product_name, price FROM daily_price WHERE user_id=? AND world=? AND date=? AND market=?",
            (user_id, world, date, market)
        )
        rows = await cursor.fetchall()
        return [{"name": r[0], "price": r[1]} for r in rows]

    async def calculate_yields(self, user_id: str, world: str) -> None:
        date = self.get_current_date()
        cursor = await self.db.execute(
            "SELECT DISTINCT product_name FROM daily_price WHERE user_id=? AND world=? AND date=?",
            (user_id, world, date)
        )
        products = [r[0] for r in await cursor.fetchall()]

        for product in products:
            cursor = await self.db.execute(
                "SELECT market, price FROM daily_price WHERE user_id=? AND world=? AND date=? AND product_name=?",
                (user_id, world, date, product)
            )
            market_prices = await cursor.fetchall()
            if len(market_prices) < 2:
                continue
            prices = [mp[1] for mp in market_prices]
            max_price = max(prices)
            min_price = min(prices)
            max_market = [mp[0] for mp in market_prices if mp[1] == max_price][0]
            min_market = [mp[0] for mp in market_prices if mp[1] == min_price][0]
            same_profit = max_price - min_price

            cross_cursor = await self.db.execute(
                "SELECT user_id, world, market, price FROM daily_price WHERE date=? AND product_name=?",
                (date, product)
            )
            all_prices = await cross_cursor.fetchall()
            if len(all_prices) < 2:
                cross_profit = same_profit
                buy_user_val = user_id
                buy_world_val = world
                buy_market_val = min_market
                sell_user_val = user_id
                sell_world_val = world
                sell_market_val = max_market
            else:
                all_max = max(all_prices, key=lambda x: x[3])
                all_min = min(all_prices, key=lambda x: x[3])
                cross_profit = all_max[3] - all_min[3]
                buy_user_val = all_min[0]
                buy_world_val = all_min[1]
                buy_market_val = all_min[2]
                sell_user_val = all_max[0]
                sell_world_val = all_max[1]
                sell_market_val = all_max[2]

            await self.db.execute("""
                INSERT INTO daily_yield (user_id, world, date, product_name,
                    same_world_profit,
                    cross_world_profit,
                    buy_user, buy_world, buy_market,
                    sell_user, sell_world, sell_market)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id, world, date, product_name) DO UPDATE SET
                    same_world_profit=excluded.same_world_profit,
                    cross_world_profit=excluded.cross_world_profit,
                    buy_user=excluded.buy_user,
                    buy_world=excluded.buy_world,
                    buy_market=excluded.buy_market,
                    sell_user=excluded.sell_user,
                    sell_world=excluded.sell_world,
                    sell_market=excluded.sell_market
            """, (user_id, world, date, product,
                  same_profit,
                  cross_profit,
                  buy_user_val, buy_world_val, buy_market_val,
                  sell_user_val, sell_world_val, sell_market_val))
        await self.db.commit()

    async def check_historical_low(self, product_name: str, profit: float, user_id: str, world: str) -> bool:
        cursor = await self.db.execute(
            "SELECT low_yield FROM historical_low WHERE product_name=?",
            (product_name,)
        )
        row = await cursor.fetchone()
        date = self.get_current_date()
        if row is None:
            await self.db.execute(
                "INSERT INTO historical_low (product_name, low_yield, user_id, world, date) VALUES (?,?,?,?,?)",
                (product_name, profit, user_id, world, date)
            )
            await self.db.commit()
            return True
        if profit > row[0]:
            await self.db.execute(
                "UPDATE historical_low SET low_yield=?, user_id=?, world=?, date=? WHERE product_name=?",
                (profit, user_id, world, date, product_name)
            )
            await self.db.commit()
            return True
        return False

    async def get_user_worlds(self, user_id: str) -> List[str]:
        date = self.get_current_date()
        cursor = await self.db.execute(
            "SELECT DISTINCT world FROM daily_price WHERE user_id=? AND date=?",
            (user_id, date)
        )
        return [r[0] for r in await cursor.fetchall()]

    async def get_user_records(self, user_id: str) -> Dict:
        date = self.get_current_date()
        cursor = await self.db.execute(
            "SELECT world, market, product_name, price FROM daily_price WHERE user_id=? AND date=? ORDER BY world, market",
            (user_id, date)
        )
        rows = await cursor.fetchall()
        result = {}
        for world, market, product, price in rows:
            if world not in result:
                result[world] = {}
            if market not in result[world]:
                result[world][market] = []
            result[world][market].append({"name": product, "price": price})
        return result

    async def get_best_yields(self) -> Dict[str, Dict]:
        date = self.get_current_date()
        cursor = await self.db.execute(
            "SELECT product_name, world, user_id, same_world_profit, buy_user, buy_world, buy_market, sell_user, sell_world, sell_market, cross_world_profit FROM daily_yield WHERE date=?",
            (date,)
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            product = row[0]
            world = row[1]
            user_id = row[2]
            if product not in result:
                result[product] = {
                    "same_world": {"profit": row[3], "world": world, "user_id": user_id},
                    "cross_world": {
                        "buy_user": row[4], "buy_world": row[5], "buy_market": row[6],
                        "sell_user": row[7], "sell_world": row[8], "sell_market": row[9],
                        "profit": row[10]
                    }
                }
            else:
                if row[3] is not None and (result[product]["same_world"]["profit"] is None or row[3] > result[product]["same_world"]["profit"]):
                    result[product]["same_world"] = {"profit": row[3], "world": world, "user_id": user_id}
                if row[10] is not None and (result[product]["cross_world"]["profit"] is None or row[10] > result[product]["cross_world"]["profit"]):
                    result[product]["cross_world"] = {
                        "buy_user": row[4], "buy_world": row[5], "buy_market": row[6],
                        "sell_user": row[7], "sell_world": row[8], "sell_market": row[9],
                        "profit": row[10]
                    }
        return result

    async def get_historical_lows(self) -> List[Dict]:
        cursor = await self.db.execute(
            "SELECT product_name, low_yield, user_id, world, date FROM historical_low ORDER BY low_yield DESC"
        )
        rows = await cursor.fetchall()
        return [{"product": r[0], "yield": r[1], "user_id": r[2], "world": r[3], "date": r[4]} for r in rows]
