import asyncio
import serial_asyncio

class SerialBridge:
    def __init__(self, ack_q: asyncio.Queue, qr_q: asyncio.Queue, port: str = "/dev/ttyACM0", baud: int = 115200):
        self.ack_q = ack_q
        self.qr_q  = qr_q
        self.port  = port
        self.baud  = baud
        self.reader = self.writer = None

    async def run(self):
        self.reader, self.writer = await serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baud)
        while True:
            line = (await self.reader.readline()).decode().strip()
            if line == "OK":
                await self.ack_q.put(line)
            else:
                await self.qr_q.put(line)

    async def send_recipe(self, recipe: dict):
        csv = ",".join(map(str, recipe["ml"])) + f",{recipe['idx']}\n"
        self.writer.write(csv.encode())