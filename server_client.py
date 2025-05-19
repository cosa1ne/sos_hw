import aiohttp, asyncio, json, websockets, time

API_HOST = "http://<host>"           # 변경 필요
WS_URI   = "ws://<host>/api.scentofsound.com"

class ServerClient:
    def __init__(self, recipe_q: asyncio.Queue):
        self.recipe_q = recipe_q
        self.session_id: str | None = None

    async def post_scan(self, value: str) -> str:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{API_HOST}/api/v1/scan", json={"value": value, "ts": int(time.time()*1000)})
            data = await r.json()
            self.session_id = data["sessionId"]
            return self.session_id

    async def run(self):
        while True:
            if not self.session_id:
                await asyncio.sleep(1); continue
            try:
                async with websockets.connect(WS_URI) as ws:
                    await ws.send(json.dumps({"sessionId": self.session_id}))
                    async for raw in ws:
                        await self.recipe_q.put(json.loads(raw))
            except Exception as e:
                print(f"[WS] reconnect: {e}")
                await asyncio.sleep(2)