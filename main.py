import asyncio, signal
from sent_of_sound.server_client import ServerClient
from serial_bridge import SerialBridge
from printer import print_receipt

RECIPE_Q = asyncio.Queue()
ACK_Q    = asyncio.Queue()
QR_Q     = asyncio.Queue()

async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    server = ServerClient(recipe_q=RECIPE_Q)
    serial = SerialBridge(ack_q=ACK_Q, qr_q=QR_Q)

    asyncio.create_task(server.run())
    asyncio.create_task(serial.run())

    while True:
        qr = await QR_Q.get()
        print(f"[QR] {qr}")
        session_id = await server.post_scan(qr)
        print(f"[SESSION] {session_id}")

        recipe = await RECIPE_Q.get()
        await serial.send_recipe(recipe)

        ack = await ACK_Q.get()
        if ack == "OK":
            print_receipt(recipe)

if __name__ == "__main__":
    asyncio.run(main())