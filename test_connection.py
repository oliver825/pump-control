"""
RUN THIS FIRST.

In PyCharm: right-click this file in the left-hand file list and choose
"Run 'test_connection'". No settings, no arguments, no typing needed.

It will:
  1. list the COM ports on this laptop
  2. ask which one the pump is on
  3. prove the pump answers
  4. work out which speed-command format your pump firmware wants

If step 3 fails, no amount of Python will help until the cable and the pump's
mode are sorted out. Fix that before touching anything else.
"""

import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit(
        "\n  pyserial is not installed for this interpreter.\n\n"
        "  In PyCharm: View > Tool Windows > Terminal, then type:\n"
        "      pip install pyserial openpyxl\n\n"
        "  Careful: the package is 'pyserial', NOT 'serial'. Installing\n"
        "  'serial' gives you a different, broken package.\n")


def choose_port():
    ports = list(list_ports.comports())
    if not ports:
        print("""
No COM ports found on this laptop at all.

The pump speaks RS232, so the USB cable is going through a USB-to-serial
adapter. If one is plugged in and nothing shows up here:

  1. Open Device Manager (press the Windows key, type "device manager")
  2. Look under "Ports (COM & LPT)"
  3. If there is nothing there, look for a device with a yellow warning
     triangle. That means the adapter's driver is missing.

Nothing else in this project will work until a COM port appears here.
""")
        return None

    print(f"\nFound {len(ports)} COM port(s):\n")
    for i, p in enumerate(ports, 1):
        print(f"  [{i}]  {p.device:<8} {p.description}")
    print("  [0]  quit\n")

    if len(ports) == 1:
        print("Only one port, so that is almost certainly the pump.")

    while True:
        raw = input("Type the number of the port to test and press Enter: ").strip()
        if raw in ("0", "q", ""):
            return None
        try:
            n = int(raw)
            if 1 <= n <= len(ports):
                return ports[n - 1].device
        except ValueError:
            pass
        print("  Type one of the numbers in square brackets.")


def talk(port):
    print(f"\nOpening {port} at 9600 8-N-2 ...")
    ser = serial.Serial(port, 9600, bytesize=8, parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_TWO, timeout=0, write_timeout=2)
    time.sleep(0.3)
    ser.reset_input_buffer()

    def send(cmd, wait=1.0):
        print(f"\n  TX  {cmd!r}")
        ser.reset_input_buffer()
        ser.write(cmd.encode() + b"\r")
        end = time.time() + wait
        buf = b""
        while time.time() < end:
            n = ser.in_waiting
            if n:
                buf += ser.read(n)
                end = time.time() + 0.15       # extend while data keeps coming
            time.sleep(0.01)
        print(f"  RX  {buf!r}")
        return buf

    reply = send("1RS")
    if not reply:
        print("""
NOTHING CAME BACK. In order of likelihood:

  1. THE PUMP IS NOT IN SERIAL MODE. Press the MODE key on the pump until the
     display shows "dig". Nothing on the serial port works until it does.
     This is the answer about eight times out of ten.
  2. Wrong COM port. Run this again and try another number from the list.
  3. The old LabVIEW program, PuTTY, or another terminal still has the port
     open. Close them and try again.
  4. TX and RX swapped. Most USB-to-serial adapters are wired straight
     through, but the pump may need a null-modem adapter.
  5. Wrong pump. Only the 323Du has RS232. A 323E, 323S or 323U does not.
""")
        ser.close()
        return

    text = reply.decode("ascii", "replace").strip()
    print(f"\n  THE PUMP IS TALKING. It said: {text}")
    print("  That decodes as: [model] [speed] [direction] [1=running 0=stopped] !")

    # The manual writes the speed command as 1SPxxx, implying three digits.
    # Some firmware accepts a plain integer instead. Find out which.
    print("\nTesting the two possible speed-command formats.")
    print("The pump stays stopped for this, it only changes the set speed.\n")
    results = {}
    for label, cmd in (("zero padded (1SP020)", "1SP020"),
                       ("plain (1SP20)", "1SP20")):
        send(cmd)
        status = send("1RS").decode("ascii", "replace")
        results[label] = "20" in status.split("!")[0]

    print("\n  " + "-" * 46)
    for label, ok in results.items():
        print(f"  {label:<26} {'ACCEPTED' if ok else 'ignored'}")
    print("  " + "-" * 46)

    if results["zero padded (1SP020)"]:
        print("\n  Nothing to change. wm323.py already sends the zero-padded form.")
    elif results["plain (1SP20)"]:
        print("\n  ACTION NEEDED: open wm323.py and change")
        print('      speed_format: str = "{:03d}"')
        print("  to")
        print('      speed_format: str = "{:d}"')
    else:
        print("\n  Neither format took. The pump may be in the wrong mode, or it")
        print("  may refuse speed changes while stopped. Try the spin test below.")

    ans = input("\nSpin the pump at 20 rpm for 5 seconds? "
                "MAKE SURE THE OUTLET IS SAFE. (y/N): ").strip().lower()
    if ans == "y":
        print("\nSpinning. Ctrl-C stops it early.")
        try:
            send("1RR")
            send("1GO")
            time.sleep(5)
        except KeyboardInterrupt:
            print("  interrupted")
        finally:
            send("1ST")
            print("  stopped")

    send("1ST")
    ser.close()
    print("\nDone. Port closed, pump stopped.")
    print("If the pump answered, you are ready to run pump_app.py.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    port = args[0] if args else choose_port()
    if port:
        try:
            talk(port)
        except serial.SerialException as e:
            print(f"\nCould not open the port: {e}\n")
            print("Something else probably has it open already. Close the old")
            print("LabVIEW program, PuTTY, or any other terminal and try again.")
    input("\nPress Enter to close.")
