#!/usr/bin/env python3

"""
config_gen.py - stratum config generation

Usage:
    python tools/config_gen.py                          # defaults
    python tools/config_gen.py --config config.toml     # explicit path
    python tools/config_gen.py --dry-run                # validation only
"""

import argparse
import sys
from pathlib import Path
from typing import Literal

import tomllib
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field, ValidationError

JINJA2_TEMPLATE = Path(__file__).parent / "config_template.c.j2"
DEFAULT_CONFIG_TOML = Path(__file__).parent.parent / "config.toml"


class RP2040Capabilities:
    """Constants and constraints defining RP2040 hardware limits."""

    GPIO_MIN = 0
    GPIO_MAX = 29
    GPIO_NUM = 30

    PIO_NUM = 2
    SM_PER_PIO = 4
    INSTR_NUM = 32

    INSTR_FOOTPRINT = {
        "swd": 10,
        "spi": 10,
        "i2c": 10,
        "uart": 10,
    }

    TARGET_NAME_LEN = 32
    SWD_NAME_LEN = 8
    SNIFFER_NAME_LEN = 8

    TYPE_ENUM = {
        "spi": "SNIFFER_TYPE_SPI",
        "i2c": "SNIFFER_TYPE_I2C",
        "uart": "SNIFFER_TYPE_UART",
    }


class BasePeripheralModel(BaseModel):
    """Base class for all peripheral models (SWD, Sniffers)."""

    name: str
    pio_id: int | None = None
    sm_id: int | None = None

    def get_pins(self) -> list[int | None]:
        """Get a list of currently configured pins for this peripheral."""
        raise NotImplementedError

    def set_pins(self, pins: list[int]) -> None:
        """Set the final allocated pins for this peripheral."""
        raise NotImplementedError

    @property
    def ptype(self) -> str:
        """Return the peripheral type string."""
        raise NotImplementedError


class SwdModel(BasePeripheralModel):
    """Configuration model for SWD (Serial Wire Debug) targets."""

    model_config = {"arbitrary_types_allowed": True}

    name: str = Field(min_length=1, max_length=RP2040Capabilities.SWD_NAME_LEN - 1)
    clk_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    io_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    freq_hz: int = Field(default=1_000_000, gt=0, le=24_000_000)

    def get_pins(self) -> list[int | None]:
        return [self.clk_pin, self.io_pin]

    def set_pins(self, pins: list[int]) -> None:
        self.clk_pin, self.io_pin = pins[0], pins[1]

    @property
    def ptype(self) -> str:
        return "swd"


class SpiModel(BasePeripheralModel):
    """Configuration model for SPI sniffer instances."""

    type: Literal["spi"]
    name: str = Field(min_length=1, max_length=RP2040Capabilities.SNIFFER_NAME_LEN - 1)
    clk_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    mosi_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    miso_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    cs_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    txn_width: Literal[4, 8, 16, 32] = 8

    def get_pins(self) -> list[int | None]:
        return [self.clk_pin, self.mosi_pin, self.miso_pin, self.cs_pin]

    def set_pins(self, pins: list[int]) -> None:
        self.clk_pin, self.mosi_pin, self.miso_pin, self.cs_pin = (
            pins[0],
            pins[1],
            pins[2],
            pins[3],
        )

    @property
    def ptype(self) -> str:
        return "spi"


class I2cModel(BasePeripheralModel):
    """Configuration model for I2C sniffer instances."""

    type: Literal["i2c"]
    name: str = Field(min_length=1, max_length=RP2040Capabilities.SNIFFER_NAME_LEN - 1)
    scl_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    sda_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    addr_filter: int = Field(default=0x00, ge=0x00, le=0x7F)

    def get_pins(self) -> list[int | None]:
        return [self.scl_pin, self.sda_pin]

    def set_pins(self, pins: list[int]) -> None:
        self.scl_pin, self.sda_pin = pins[0], pins[1]

    @property
    def ptype(self) -> str:
        return "i2c"


class UartModel(BasePeripheralModel):
    """Configuration model for UART sniffer instances."""

    type: Literal["uart"]
    name: str = Field(min_length=1, max_length=RP2040Capabilities.SNIFFER_NAME_LEN - 1)
    tx_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    rx_pin: int | None = Field(
        default=None, ge=RP2040Capabilities.GPIO_MIN, le=RP2040Capabilities.GPIO_MAX
    )
    baud_rate: int = Field(default=115200, ge=0)

    def get_pins(self) -> list[int | None]:
        return [self.tx_pin, self.rx_pin]

    def set_pins(self, pins: list[int]) -> None:
        self.tx_pin, self.rx_pin = pins[0], pins[1]

    @property
    def ptype(self) -> str:
        return "uart"


SnifferModel = SpiModel | I2cModel | UartModel


class TargetModel(BaseModel):
    """Top-level configuration model for a stratum target."""

    name: str = Field(min_length=1, max_length=RP2040Capabilities.TARGET_NAME_LEN - 1)
    swd: list[SwdModel] = Field(default=[])
    sniffer: list[SnifferModel] = Field(default=[])


class ConfigError(ValueError):
    """Raised when configuration validation or resource allocation fails."""

    pass


class ResourceAllocator:
    """Manages allocation of PIO state machines, instruction memory, and GPIO pins."""

    def __init__(self):
        """Initialize allocator with empty resource pools."""
        self.pio_mem = [0, 0]
        self.pio_sms = [0, 0]
        self.loaded = [set(), set()]
        self.used_pins: dict[int, str] = {}
        self.sm_tasks = [[], []]

    def allocate(self, ptype: str, item_name: str, manual_pins: list):
        """
        Attempt to allocate PIO resources and pins for a given task.

        Args:
            ptype: Type of task ('swd', 'spi', 'i2c', 'uart').
            item_name: Unique name of the task instance.
            manual_pins: List of requested pin numbers (None for auto-allocation).

        Returns:
            Tuple of (pio_id, sm_id, allocated_pins).

        Raises:
            ConfigError: If pin allocation fails.
            RuntimeError: If PIO resources are exhausted.
        """
        cost = RP2040Capabilities.INSTR_FOOTPRINT[ptype]

        for b in range(RP2040Capabilities.PIO_NUM):
            mem_tax = cost if ptype not in self.loaded[b] else 0

            if (
                self.pio_mem[b] + mem_tax <= RP2040Capabilities.INSTR_NUM
                and self.pio_sms[b] < RP2040Capabilities.SM_PER_PIO
            ):
                final_pins = []
                local_claimed = set()

                for pin in manual_pins:
                    if pin is not None:
                        if pin in self.used_pins:
                            raise ConfigError(f"pin {pin} already allocated")
                        final_pins.append(pin)
                        local_claimed.add(pin)
                    else:
                        first_unused_pin = next(
                            (
                                pin
                                for pin in range(RP2040Capabilities.GPIO_NUM)
                                if pin not in self.used_pins
                                and pin not in local_claimed
                            ),
                            None,
                        )
                        if first_unused_pin is None:
                            break
                        final_pins.append(first_unused_pin)
                        local_claimed.add(first_unused_pin)

                if len(final_pins) < len(manual_pins):
                    continue

                self.pio_mem[b] += mem_tax
                self.pio_sms[b] += 1
                self.loaded[b].add(ptype)
                self.sm_tasks[b].append((item_name, cost))
                for pin in local_claimed:
                    self.used_pins[pin] = f"{item_name}({ptype})"
                return b, self.pio_sms[b] - 1, final_pins

        raise RuntimeError(f"resource exhaustion: {item_name}")


class ResourceVisualizer:
    """Provides ASCII-art visualization of resource allocation."""

    @staticmethod
    def draw_board(alloc: ResourceAllocator) -> None:
        """Draw an ASCII representation of GPIO pin usage."""
        # Title centering: 13 (indent) + 28 (box) + 12 (indent) = 53. (53-15)/2 = 19.
        print("\n" + " " * 19 + "GPIO ALLOCATION")
        print(" " * 13 + "┌" + "─" * 26 + "┐")

        for i in range(15):
            lp, rp = i, i + 15
            l_stat = "■ " if lp in alloc.used_pins else "  "
            r_stat = " ■" if rp in alloc.used_pins else "  "
            l_name = alloc.used_pins.get(lp, "").split("(")[0][:10].rjust(10)
            r_name = alloc.used_pins.get(rp, "").split("(")[0][:10].ljust(10)

            print(
                f"{l_name} {l_stat}│({lp:02})"
                + " " * 18
                + f"({rp:02})│{r_stat} {r_name}"
            )

        print(" " * 13 + "└" + "─" * 26 + "┘")

    @staticmethod
    def draw_pio_resources(alloc: ResourceAllocator) -> None:
        """Draw an ASCII representation of PIO instruction and state machine usage."""
        # Title centering: PIO block is 80 chars wide. (80-14)/2 = 33.
        print("\n" + " " * 33 + "PIO ALLOCATION")
        print(
            "\n"
            + " " * 4
            + "┌────────────── PIO 0 ──────────────┐   ┌────────────── PIO 1 ──────────────┐"
        )
        instr0 = f"{alloc.pio_mem[0]}/32 Instr".center(35)
        instr1 = f"{alloc.pio_mem[1]}/32 Instr".center(35)
        print(f"    │{instr0}│   │{instr1}│")
        print(
            "    ├────────┬────────┬────────┬────────┤   ├────────┬────────┬────────┬────────┤"
        )

        def get_task_line(pio_idx, line_type):
            parts = []
            for i in range(RP2040Capabilities.SM_PER_PIO):
                if i < len(alloc.sm_tasks[pio_idx]):
                    name, cost = alloc.sm_tasks[pio_idx][i]
                    if line_type == "name":
                        parts.append(name[:8].center(8))
                    else:
                        parts.append(f"({cost:02})".center(8))
                else:
                    parts.append("  --    ")
            return "│" + "│".join(parts) + "│"

        print(f"    {get_task_line(0, 'name')}   {get_task_line(1, 'name')}")
        print(f"    {get_task_line(0, 'cost')}   {get_task_line(1, 'cost')}")
        print(
            "    └────────┴────────┴────────┴────────┘   └────────┴────────┴────────┴────────┘"
        )
        print("\n")


def generate_c_config(target_config: TargetModel):
    """
    Generate the config.c file from the allocated target configuration.

    Args:
        target_config: The TargetModel containing allocated resources and pins.
    """
    jinja2_env = Environment(loader=FileSystemLoader(str(JINJA2_TEMPLATE.parent)))
    jinja2_template = jinja2_env.get_template(JINJA2_TEMPLATE.name)
    jinja2_op = jinja2_template.render(target=target_config)

    output_path = Path(__file__).parent.parent / "firmware" / "config.c"
    with open(output_path, "w", encoding="utf-8") as config_file:
        config_file.write(jinja2_op)

    print(f"[config_gen] successfully generated {output_path}")


def main():
    """Main entry point for the stratum config generator CLI."""
    parser = argparse.ArgumentParser(description="stratum config generator")
    _ = parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_TOML,
        help=f"Path to the configuration TOML file (default: {DEFAULT_CONFIG_TOML})",
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and allocate resources without generating C code",
    )
    args = parser.parse_args()

    print("=== stratum config generator ===")
    print(f"[config_gen] reading: {args.config}")

    try:
        with open(args.config, "rb") as config_file:
            user_config = tomllib.load(config_file)
    except FileNotFoundError:
        print(f"[config_gen] error: {args.config} not found")
        sys.exit(1)
    except Exception as e:
        print(f"[config_gen] TOML syntax error: {e}")
        sys.exit(1)

    try:
        target = TargetModel(**user_config)
    except ValidationError as e:
        print("[config_gen] config validation failed")
        for error in e.errors():
            loc = " -> ".join(str(x) for x in error["loc"])
            print(f"  - [{loc}]: {error['msg']}")
        sys.exit(1)

    alloc = ResourceAllocator()
    tasks: list[BasePeripheralModel] = [s for s in target.swd] + [
        s for s in target.sniffer
    ]
    tasks.sort(
        key=lambda x: RP2040Capabilities.INSTR_FOOTPRINT.get(x.ptype, 0), reverse=True
    )

    try:
        for item in tasks:
            pio_idx, sm_idx, pins = alloc.allocate(
                item.ptype, item.name, item.get_pins()
            )

            item.pio_id, item.sm_id = pio_idx, sm_idx
            item.set_pins(pins)

        viz = ResourceVisualizer()
        viz.draw_board(alloc)
        viz.draw_pio_resources(alloc)

        if not args.dry_run:
            generate_c_config(target)
        else:
            print("[config_gen] dry-run: skipping code generation")

    except RuntimeError as e:
        print(f"[config_gen] resource error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
