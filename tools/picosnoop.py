"""
PICOSnoop: An interactive memory explorer and flasher for the Raspberry Pi Pico (RP2040).

Provides a REPL and CLI interface to read memory, dump registers safely, and load UF2/ELF
firmware directly into SRAM using the PICOBOOT USB protocol.
"""

import argparse
import os
import struct
import subprocess
import sys
import usb.core
import usb.util

try:
    import readline
except ImportError:
    readline = None

PICOBOOT_MAGIC = 0x431FD10B

# PICOBOOT Command IDs
PC_EXCLUSIVE_ACCESS = 0x01
PC_REBOOT = 0x02
PC_FLASH_ERASE = 0x03
PC_READ = 0x84
PC_WRITE = 0x05
PC_EXIT_XIP = 0x06
PC_ENTER_CMD_XIP = 0x07
PC_EXEC = 0x08

# Raspberry Pi Pico HW IDs
VID = 0x2E8A
PID = 0x0003


class Logger:
    """Provides colorized console logging capabilities."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"

    @classmethod
    def info(cls, msg):
        """Log an informational message."""
        print(f"{cls.GREEN}[INFO]{cls.RESET} {msg}", file=sys.stderr)

    @classmethod
    def warn(cls, msg):
        """Log a warning message."""
        print(f"{cls.YELLOW}[WARN]{cls.RESET} {msg}", file=sys.stderr)

    @classmethod
    def error(cls, msg):
        """Log an error message."""
        print(f"{cls.RED}[ERROR]{cls.RESET} {msg}", file=sys.stderr)


def print_progress(current, total, bar_length=40):
    """
    Render a text-based progress bar to stderr.

    Args:
        current (int): The current progress value.
        total (int): The total expected value.
        bar_length (int): Character width of the progress bar.
    """
    if total <= 0:
        return
    percent = float(current) * 100 / total
    filled = int(bar_length * current // total)
    bar = "█" * filled + "-" * (bar_length - filled)
    sys.stderr.write(
        f"\r{Logger.BLUE}[PROGRESS]{Logger.RESET} |{bar}| {percent:.1f}% ({current}/{total} bytes)"
    )
    if current >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def find_picoboot_device():
    """
    Locate the RP2040 device in PICOBOOT mode on the USB bus.

    Returns:
        usb.core.Device: The PyUSB device object, or None if not found.
    """
    return usb.core.find(idVendor=VID, idProduct=PID)


def get_endpoints(dev):
    """
    Extract the vendor-specific IN and OUT endpoints from the USB device.

    Args:
        dev (usb.core.Device): The PyUSB device object.

    Returns:
        tuple: (interface, out_endpoint, in_endpoint) or (None, None, None) if not found.
    """
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0xFF:
                out_ep = in_ep = None
                for ep in intf:
                    if (
                        usb.util.endpoint_direction(ep.bEndpointAddress)
                        == usb.util.ENDPOINT_OUT
                    ):
                        out_ep = ep
                    else:
                        in_ep = ep
                if out_ep and in_ep:
                    return intf, out_ep, in_ep
    return None, None, None


def send_cmd(out_ep, in_ep, cmd_id, args=b"", transfer_len=0):
    """
    Format and send a standard 32-byte PICOBOOT command packet over USB.

    Args:
        out_ep: The PyUSB OUT endpoint.
        in_ep: The PyUSB IN endpoint (unused here but matches convention).
        cmd_id (int): The PICOBOOT command ID.
        args (bytes): Command-specific payload (up to 16 bytes).
        transfer_len (int): Length of the data phase to follow.
    """
    token = 1
    # PICOBOOT Command Structure (32 bytes):
    # uint32_t magic, uint32_t token, uint8_t cmd_id, uint8_t cmd_size,
    # uint16_t _unused, uint32_t transfer_len, uint8_t args[16]
    header = struct.pack(
        "<IIBBHI", PICOBOOT_MAGIC, token, cmd_id, len(args), 0, transfer_len
    )
    full_cmd = header + args + b"\x00" * (16 - len(args))
    out_ep.write(full_cmd, timeout=3000)


def write_memory(out_ep, in_ep, address, data, show_progress=False):
    """
    Write a block of data to the specified memory address on the device.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        address (int): The target 32-bit memory address.
        data (bytes): The data payload to write.
        show_progress (bool): If True, display a progress bar during transfer.
    """
    args = struct.pack("<II", address, len(data))
    send_cmd(out_ep, in_ep, PC_WRITE, args, len(data))

    chunk_size = 4096
    for i in range(0, len(data), chunk_size):
        chunk = data[i : i + chunk_size]
        out_ep.write(chunk, timeout=5000)
        if show_progress:
            print_progress(i + len(chunk), len(data))

    try:
        in_ep.read(64, timeout=1000)  # Read empty ACK packet
    except Exception:
        pass


def _read_memory_raw(out_ep, in_ep, address, size, show_progress=False):
    """
    Perform a standard bulk memory read from the device.
    Note: Can cause bus faults if used on non-memory addresses (e.g., peripheral registers).

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        address (int): The target 32-bit memory address.
        size (int): Number of bytes to read.
        show_progress (bool): If True, display a progress bar.

    Returns:
        bytes: The read memory content.
    """
    args = struct.pack("<II", address, size)
    send_cmd(out_ep, in_ep, PC_READ, args, size)

    data = bytearray()
    while len(data) < size:
        chunk = in_ep.read(min(size - len(data), 4096), timeout=5000)
        data.extend(chunk)
        if show_progress:
            print_progress(len(data), size)

    try:
        out_ep.write(b"", timeout=1000)  # Send empty ACK packet
    except Exception:
        pass
    return bytes(data)


def exec_address(out_ep, in_ep, address):
    """
    Instruct the Bootrom to execute code at the given address.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        address (int): The memory address to branch to.
    """
    args = struct.pack("<I", address)
    send_cmd(out_ep, in_ep, PC_EXEC, args)
    try:
        in_ep.read(64, timeout=1000)  # Read ACK
    except Exception:
        pass


def read_memory_safe(out_ep, in_ep, address, size):
    """
    Safely read peripheral registers (APB/AHB) word-by-word.
    Avoids bus faults caused by bulk reads on non-RAM/Flash addresses by injecting
    a small 12-byte assembly payload into SRAM and asking the Bootrom to execute it.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        address (int): Target memory address (usually >= 0x40000000).
        size (int): Number of bytes to read.

    Returns:
        bytes: The read register content.
    """
    data = bytearray()
    # Payload: ldr r0, [pc, #8]; ldr r0, [r0, #0]; mov r1, pc; str r0, [r1, #4]; bx lr; nop
    peek_cmd = b"\x02\x48\x00\x68\x79\x46\x48\x60\x70\x47\xc0\x46"
    scratch = 0x20000000

    Logger.info(f"Reading {size} bytes via EXEC peek...")
    words = (size + 3) // 4
    for i in range(words):
        curr = address + (i * 4)
        write_memory(out_ep, in_ep, scratch, peek_cmd + struct.pack("<I", curr))
        exec_address(out_ep, in_ep, scratch)
        data.extend(_read_memory_raw(out_ep, in_ep, scratch + 12, 4))
        print_progress(len(data), words * 4)
    return bytes(data[:size])


def hex_dump(data, start_address):
    """
    Print a colorized, formatted hex dump of the provided data block to stdout.

    Args:
        data (bytes): The data to print.
        start_address (int): The logical starting memory address of the chunk.
    """
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hs = ""
        for j in range(16):
            if j < len(chunk):
                b = chunk[j]
                if b == 0:
                    hs += "\033[2m00\033[0m "
                elif b == 0xFF:
                    hs += f"{Logger.YELLOW}ff{Logger.RESET} "
                else:
                    hs += f"{b:02x} "
            else:
                hs += "   "
            if j == 7:
                hs += " "
        as_ = "".join(
            f"{Logger.GREEN}{chr(b)}{Logger.RESET}"
            if 32 <= b <= 126
            else "\033[2m.\033[0m"
            for b in chunk
        )
        print(
            f"{Logger.CYAN}{start_address + i:08x}{Logger.RESET}  {hs} {Logger.BLUE}|{Logger.RESET}{as_}{Logger.BLUE}|{Logger.RESET}"
        )


def setup_udev_rules():
    """
    Attempt to automatically install Linux udev rules to allow non-root USB access.

    Returns:
        bool: True if installation succeeded, False otherwise.
    """
    rule = 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="0666"\n'
    path = "/etc/udev/rules.d/99-picoboot.rules"
    Logger.warn("Requesting sudo for udev rules...")
    try:
        subprocess.run(
            f"echo '{rule}' | sudo tee {path} > /dev/null", shell=True, check=True
        )
        subprocess.run(["sudo", "udevadm", "control", "--reload-rules"], check=True)
        subprocess.run(["sudo", "udevadm", "trigger"], check=True)
        Logger.info(f"Rules installed at {path}.")
        return True
    except Exception:
        return False


def check_and_prompt_udev():
    """
    Check if the user lacks USB permissions on Linux and interactively prompt them
    to install the required udev rules.

    Returns:
        bool: False. Calls sys.exit(0) if user agrees and installation succeeds.
    """
    if sys.platform != "linux":
        return False
    print()
    Logger.error("Permission denied.")
    resp = (
        input(f"{Logger.BOLD}Install udev rules? [y/N]: {Logger.RESET}").strip().lower()
    )
    if resp in ("y", "yes") and setup_udev_rules():
        Logger.info("Re-plug your Pico and try again.")
        sys.exit(0)
    return False


def reboot_device(out_ep, in_ep, pc=0, sp=0):
    """
    Issue a software reboot command to the Bootrom.
    If pc is provided and is an even address, it triggers a Vector Table Boot logic sequence.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        pc (int): Optional Program Counter / Vector Table address.
        sp (int): Optional Stack Pointer.
    """
    if pc != 0 and sp == 0:
        sp = 0x20042000

    delay_ms = 500 if pc != 0 else 0
    args = struct.pack("<III", pc, sp, delay_ms)
    send_cmd(out_ep, in_ep, PC_REBOOT, args)

    if pc != 0:
        Logger.info(f"Reboot command sent (PC=0x{pc:08x}, SP=0x{sp:08x}).")
    else:
        Logger.info("Standard Reboot command sent.")


def enter_exclusive(out_ep, in_ep):
    """
    Lock the Bootrom into an exclusive command execution state.
    """
    send_cmd(out_ep, in_ep, PC_EXCLUSIVE_ACCESS, b"\x01")
    try:
        in_ep.read(64, timeout=500)
    except Exception:
        pass


def load_uf2(out_ep, in_ep, filename):
    """
    Parse a UF2 file, extract all blocks targeting SRAM, upload them to the device,
    and trigger a reboot sequence at the lowest loaded address.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        filename (str): Path to the .uf2 file.
    """
    if not os.path.isfile(filename):
        return
    chunks = []
    with open(filename, "rb") as f:
        while True:
            block = f.read(512)
            if len(block) < 512:
                break
            h = struct.unpack("<IIIIIIII", block[:32])
            if (
                h[0] == 0x0A324655
                and h[1] == 0x9E5D5157
                and 0x20000000 <= h[3] < 0x20042000
            ):
                chunks.append((h[3], block[32 : 32 + h[4]]))
    if not chunks:
        Logger.error("No SRAM-targeted chunks found in UF2.")
        return

    enter_exclusive(out_ep, in_ep)
    chunks.sort(key=lambda x: x[0])

    Logger.info(f"Loading {len(chunks)} blocks to SRAM...")
    for addr, data in chunks:
        write_memory(out_ep, in_ep, addr, data)

    # Bootrom UF2 behavior: reboot using the lowest uploaded segment
    pc = chunks[0][0]
    reboot_device(out_ep, in_ep, pc=pc)


def load_elf(out_ep, in_ep, filename):
    """
    Parse a 32-bit Little Endian ELF file, extract all PT_LOAD segments targeting SRAM,
    slice them into 256-byte chunks, upload them, and trigger a reboot sequence.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        filename (str): Path to the .elf file.
    """
    if not os.path.isfile(filename):
        return
    with open(filename, "rb") as f:
        h = f.read(52)
        if h[:4] != b"\x7fELF":
            Logger.error("Not a valid ELF file.")
            return
        e_entry, e_phoff = struct.unpack("<II", h[24:32])
        e_phentsize, e_phnum = struct.unpack("<HH", h[42:46])

        pages = {}
        PAGE_SIZE = 256

        # Extract PT_LOAD segments
        for i in range(e_phnum):
            f.seek(e_phoff + i * e_phentsize)
            ph = struct.unpack("<IIIIII", f.read(24))
            if ph[0] == 1 and ph[4] > 0:  # Type 1 = PT_LOAD
                p_offset, p_vaddr, p_filesz, p_memsz = ph[1], ph[2], ph[4], ph[5]
                mapped_size = min(p_filesz, p_memsz)

                if mapped_size > 0 and (0x20000000 <= p_vaddr < 0x20042000):
                    addr = p_vaddr
                    remaining = mapped_size
                    file_offset = p_offset

                    # Slice into 256-byte aligned pages
                    while remaining > 0:
                        off = addr & (PAGE_SIZE - 1)
                        length = min(remaining, PAGE_SIZE - off)
                        page_addr = addr - off

                        if page_addr not in pages:
                            pages[page_addr] = bytearray(PAGE_SIZE)

                        f.seek(file_offset)
                        frag_data = f.read(length)
                        pages[page_addr][off : off + length] = frag_data

                        addr += length
                        file_offset += length
                        remaining -= length

        if not pages:
            Logger.error("No SRAM-targeted chunks found in ELF.")
            return

        enter_exclusive(out_ep, in_ep)
        sorted_pages = sorted(pages.items())

        Logger.info(f"Loading {len(sorted_pages)}x 256-byte pages to SRAM...")
        for page_addr, page_data in sorted_pages:
            write_memory(out_ep, in_ep, page_addr, bytes(page_data))

        # Replicate Bootrom logic: reboot at the lowest loaded segment
        pc = sorted_pages[0][0]
        reboot_device(out_ep, in_ep, pc=pc)


def repl_mode(out_ep, in_ep, script_files=None):
    """
    Launch the interactive PICOSnoop Read-Eval-Print Loop.

    Args:
        out_ep: PyUSB OUT endpoint.
        in_ep: PyUSB IN endpoint.
        script_files (list): Optional list of file paths containing commands to execute first.
    """
    histfile = os.path.join(os.path.expanduser("~"), ".picosnoop_history")
    if readline:
        try:
            readline.read_history_file(histfile)

            def completer(text, state):
                line = readline.get_line_buffer()
                if line.startswith(("load ", "source ", "save ", "log ")):
                    p = os.path.expanduser(text)
                    d = os.path.dirname(p) or "."
                    b = os.path.basename(p)
                    try:
                        items = os.listdir(d)
                        matches = [
                            os.path.join(os.path.dirname(text), i)
                            for i in items
                            if i.startswith(b)
                        ]
                        if state < len(matches):
                            res = matches[state]
                            return res + "/" if os.path.isdir(res) else res
                    except Exception:
                        pass
                return None

            readline.set_completer(completer)
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass

    transcript = None

    def process(raw):
        """Process a single command string."""
        nonlocal transcript
        if not raw.strip():
            return False

        if transcript:
            try:
                with open(transcript, "a") as f:
                    f.write(f"picosnoop> {raw}\n")
            except Exception:
                pass

        parts = raw.split()
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("exit", "quit", "q"):
            return True
        elif cmd == "help":
            print(
                f"{Logger.BOLD}Commands:{Logger.RESET}\n"
                f"  {Logger.CYAN}ping{Logger.RESET}                   - Check connection status\n"
                f"  {Logger.CYAN}read/dump{Logger.RESET} <addr> <size>    - Bulk/safe read and hex dump\n"
                f"  {Logger.CYAN}load{Logger.RESET} <file.elf/uf2>        - Upload binary to SRAM and boot\n"
                f"  {Logger.CYAN}reboot{Logger.RESET} [<pc> <sp>]         - Issue software reboot\n"
                f"  {Logger.CYAN}save{Logger.RESET} <addr> <sz> <file>    - Dump memory directly to binary\n"
                f"  {Logger.CYAN}source/log{Logger.RESET} <file>          - Automate / Record session transcript"
            )
        elif cmd == "ping":
            try:
                # Address 0x00000000 (Bootrom) is always readable
                _read_memory_raw(out_ep, in_ep, 0x00000000, 4)
                Logger.info("PICOBOOT is accessible and responding!")
            except Exception:
                Logger.error(
                    "PICOBOOT is not accessible! Device might be running user code or disconnected."
                )
        elif cmd == "load" and args:
            fn = os.path.expanduser(args[0])
            if fn.endswith(".uf2"):
                load_uf2(out_ep, in_ep, fn)
            else:
                load_elf(out_ep, in_ep, fn)
        elif cmd == "reboot":
            pc = int(args[0], 0) if len(args) >= 1 else 0
            sp = int(args[1], 0) if len(args) >= 2 else 0
            reboot_device(out_ep, in_ep, pc, sp)
        elif cmd == "source" and args:
            with open(os.path.expanduser(args[0]), "r") as f:
                for l in f:
                    if l.strip() and not l.startswith("#"):
                        print(f"{Logger.BOLD}picosnoop>{Logger.RESET} {l.strip()}")
                        if process(l.strip()):
                            return True
        elif cmd == "log" and args:
            if args[0].lower() == "off":
                transcript = None
            else:
                transcript = os.path.expanduser(args[0])
                with open(transcript, "a") as f:
                    f.write("=== PICOSnoop Start ===\n")
        elif cmd in ("read", "dump") and len(args) >= 2:
            addr, size = int(args[0], 0), int(args[1], 0)
            data = (
                read_memory_safe(out_ep, in_ep, addr, size)
                if addr >= 0x40000000
                else _read_memory_raw(out_ep, in_ep, addr, size, True)
            )
            hex_dump(data, addr)
        elif cmd == "save" and len(args) == 3:
            addr, size = int(args[0], 0), int(args[1], 0)
            data = _read_memory_raw(out_ep, in_ep, addr, size, True)
            with open(os.path.expanduser(args[2]), "wb") as f:
                f.write(data)
        return False

    Logger.info("Entering PICOSnoop REPL Mode.")
    print(
        f"{Logger.CYAN}    ____  _           _____                           \n"
        f"   / __ \\(_)_________/ ___/____  ____  ____  ____     \n"
        f"  / /_/ / / ___/ __ \\\\__ \\/ __ \\/ __ \\/ __ \\/ __ \\\n"
        f" / ____/ / /__/ /_/ /___/ / / / / /_/ / /_/ / /_/ /\n"
        f"/_/   /_/\\___/\\____//____/_/ /_/\\____/\\____/ .___/\n"
        f"                                          /_/       {Logger.RESET}\n"
    )

    if script_files:
        for f in script_files:
            process(f"source {f}")

    prompt = f"\x01{Logger.GREEN}{Logger.BOLD}\x02picosnoop>\x01{Logger.RESET}\x02 "

    while True:
        try:
            line = input(prompt).strip()
            if process(line):
                break
        except (KeyboardInterrupt, EOFError):
            print()
            break

    if readline:
        readline.write_history_file(histfile)


def main():
    parser = argparse.ArgumentParser(description="PICOSnoop: RP2040 Memory Explorer")
    parser.add_argument(
        "address", nargs="?", type=lambda x: int(x, 0), help="Memory address to read"
    )
    parser.add_argument(
        "size", nargs="?", type=lambda x: int(x, 0), help="Size to read in bytes"
    )
    parser.add_argument(
        "-x", "--execute", action="append", help="Execute script file in REPL mode"
    )
    args = parser.parse_args()

    dev = find_picoboot_device()
    if not dev:
        Logger.error(
            "Pico not found in PICOBOOT mode. (Hold BOOTSEL while plugging in)"
        )
        sys.exit(1)

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        if e.errno == 13:
            check_and_prompt_udev()

    intf, out_ep, in_ep = get_endpoints(dev)
    if not out_ep:
        Logger.error("Failed to locate PICOBOOT USB endpoints.")
        sys.exit(1)

    try:
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
    except usb.core.USBError as e:
        if e.errno == 13:
            check_and_prompt_udev()
        Logger.error(f"Failed to claim interface: {e}")
        sys.exit(1)

    try:
        if args.address is None:
            if args.execute:
                for f in args.execute:
                    repl_mode(
                        out_ep, in_ep, script_files=[f]
                    )  # Pass directly to load in order
            repl_mode(out_ep, in_ep)
        else:
            if args.size is None:
                Logger.error("Must provide a size argument when executing via CLI.")
                sys.exit(1)

            if args.address >= 0x40000000:
                data = read_memory_safe(out_ep, in_ep, args.address, args.size)
            else:
                data = _read_memory_raw(out_ep, in_ep, args.address, args.size, True)

            hex_dump(data, args.address)
    finally:
        usb.util.release_interface(dev, intf.bInterfaceNumber)


if __name__ == "__main__":
    main()

