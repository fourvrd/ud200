from __future__ import annotations

import json
import os
import time
import io
import zipfile
from datetime import datetime
from enum import Enum
from typing import Dict

from construct import (
    Adapter,
    Byte,
    Bytes,
    BytesInteger,
    ByteSwapped,
    Const,
    CString,
    Computed,
    GreedyBytes,
    Int32ub,
    Padded,
    Struct,
    Switch,
    this,
)
from deepdiff import DeepDiff
from PIL import Image
from dotenv import load_dotenv

from ..device import ButtonAction, DeckDevice
from ..utils import random_string

load_dotenv()


class SmallWindowMode(Enum):
    STATS = 0
    CLOCK = 1
    BACKGROUND = 2


class CommandProtocol(Enum):
    OUT_SET_BUTTONS = 0x0001
    OUT_PARTIALLY_UPDATE_BUTTONS = 0x000d
    OUT_SET_SMALL_WINDOW_DATA = 0x0006
    OUT_SET_BRIGHTNESS = 0x000a
    OUT_SET_LABEL_STYLE = 0x000b
    IN_BUTTON = 0x0101
    IN_DEVICE_INFO = 0x0303


class LengthAdapter(Adapter):
    def _encode(self, obj, context, path):
        return obj if obj is not None else len(context.data)

    def _decode(self, obj, context, path):
        return obj


PacketStruct = Struct(
    Const(b'\x7c\x7c'),
    'command_protocol' / BytesInteger(2),
    'length' / LengthAdapter(ByteSwapped(Int32ub)),
    'data' / Padded(1024 - 8, GreedyBytes),
)

ButtonPressedStruct = Struct(
    'state' / Byte,
    'index' / Byte,
    'type' / Byte,
    'pressed_raw' / Byte,
    'pressed' / Computed(lambda ctx: ctx.type ==
                         1 if ctx.index == 13 else ctx.pressed_raw == 1),
)

IncomingStruct = Struct(
    Bytes(2),
    'command_protocol' / BytesInteger(2),
    'length' / ByteSwapped(Int32ub),
    'data' / Switch(this.command_protocol,
                    {0x0101: ButtonPressedStruct, 0x0303: CString('ascii')}),
)


class UlanziD200Device(DeckDevice):
    USB_VENDOR_ID = 0x2207
    USB_PRODUCT_ID = 0x0019
    INTERFACE_NUMBER = 0
    BUTTON_COUNT = 13
    BUTTON_ROWS = 3
    BUTTON_COLS = 5
    ICON_WIDTH = 196
    ICON_HEIGHT = 196
    DECK_NAME = 'Ulanzi Stream Controller D200'

    def __init__(self, hid_device):
        super().__init__(hid_device)
        self._small_window_mode = SmallWindowMode.CLOCK

    def keep_alive(self):
        self.set_small_window_data({})

    def set_brightness(self, brightness: int, force=False):
        if not force and brightness == self._brightness:
            return
        self._brightness = brightness
        packet = PacketStruct.build(dict(
            command_protocol=CommandProtocol.OUT_SET_BRIGHTNESS.value,
            length=None,
            data=str(brightness).encode('utf-8'),
        ))
        self._write_packet(packet)

    def set_label_style(self, label_style: Dict, force=False):
        if not force and not DeepDiff(self._label_style, label_style):
            return False

        style = {
            'Align': label_style.get('align', 'bottom'),
            'Color': int(label_style.get('color', 'FFFFFF'), 16),
            'FontName': label_style.get('font_name', 'Roboto'),
            'ShowTitle': bool(label_style.get('show_title', True)),
            'Size': label_style.get('size', 10),
            'Weight': label_style.get('weight', 80),
        }
        self._label_style = label_style
        packet = PacketStruct.build(dict(
            command_protocol=CommandProtocol.OUT_SET_LABEL_STYLE.value,
            length=None,
            data=bytearray(json.dumps(style).encode('utf-8')),
        ))
        self._write_packet(packet)

    def set_small_window_data(self, data: Dict, force=False):
        if not force and not DeepDiff(self._small_window_data, data):
            return False

        # Use provided mode or fallback to current internal mode
        mode = data.get('mode', self._small_window_mode)
        if isinstance(mode, SmallWindowMode):
            mode_val = mode.value
        else:
            mode_val = int(mode)

        cpu = data.get('cpu', 0)
        mem = data.get('mem', 0)
        gpu = data.get('gpu', 0)
        time_str = data.get('time', datetime.now().strftime('%H:%M:%S'))

        self._small_window_data = data

        # Format: "mode|cpu|mem|time|gpu"
        payload = f'{mode_val}|{cpu}|{mem}|{time_str}|{gpu}'

        packet = PacketStruct.build(dict(
            command_protocol=CommandProtocol.OUT_SET_SMALL_WINDOW_DATA.value,
            length=None,
            data=payload.encode('utf-8'),
        ))
        self._write_packet(packet)

    def set_buttons(self, buttons: Dict[int, Dict], *, update_only=False):
        """high-performance button update using in-memory ZIP."""
        zip_data = self._prepare_zip_ram(buttons)
        
        # DEBUG: save zip to cache for preview
        try:
            os.makedirs(".cache", exist_ok=True)
            with open(".cache/debug.zip", "wb") as f:
                f.write(zip_data)
        except Exception as e:
            print(f"DEBUG ERROR: failed to save debug zip: {e}")

        chunk_size = 1024
        file_size = len(zip_data)

        command = CommandProtocol.OUT_PARTIALLY_UPDATE_BUTTONS if update_only else CommandProtocol.OUT_SET_BUTTONS
        chunk = zip_data[:chunk_size - 8]
        packet = PacketStruct.build(dict(
            command_protocol=command.value,
            length=file_size,
            data=chunk.ljust(chunk_size - 8, b'\x00'),
        ))

        packets = [packet]
        for i in range(chunk_size - 8, len(zip_data), chunk_size):
            chunk = zip_data[i:i + chunk_size]
            packets.append(chunk.ljust(chunk_size, b'\x00'))

        self._write_packet(packets)

    def _prepare_zip_ram(self, buttons: Dict) -> bytes:
        """creates a zip file in RAM with all 15 buttons and cache-busting names."""
        invalid_bytes = [b'\x00', b'\x7c']
        nonce = int(time.time() * 1000)

        # Pre-convert all images to bytes to avoid repeated PIL work in loop
        processed_icons = {}
        for idx, btn in buttons.items():
            if 'icon' in btn and isinstance(btn['icon'], Image.Image):
                buf = io.BytesIO()
                btn['icon'].save(buf, format='PNG')
                processed_icons[idx] = buf.getvalue()

        dummy_retries = 0
        while dummy_retries < 50:
            zip_buffer = io.BytesIO()
            manifest = {}

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
                # Always include all 15 buttons in manifest to prevent "ghost" buttons
                for button_index in range(15):
                    row = button_index // self.BUTTON_COLS
                    col = button_index % self.BUTTON_COLS
                    
                    button = buttons.get(button_index, {})
                    button_data = {'State': 0, 'ViewParam': [{}]}
                    
                    # Text
                    button_data['ViewParam'][0]['Text'] = button.get('name', "")

                    # Icon
                    if 'icon' in button:
                        # use nonce in filename to force device refresh
                        icon_filename = f"b_{button_index}_{nonce}.png"
                        if button_index in processed_icons:
                            zf.writestr(f"icons/{icon_filename}", processed_icons[button_index])
                        elif isinstance(button['icon'], str) and os.path.exists(button['icon']):
                            with open(button['icon'], 'rb') as f:
                                zf.writestr(f"icons/{icon_filename}", f.read())
                        button_data['ViewParam'][0]['Icon'] = f'icons/{icon_filename}'
                    else:
                        button_data['ViewParam'][0]['Icon'] = ""

                    manifest[f'{col}_{row}'] = button_data

                manifest["nonce"] = nonce

                if dummy_retries > 0:
                    zf.writestr("dummy.txt", random_string(8 * dummy_retries))

                zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            zip_data = zip_buffer.getvalue()
            file_size = len(zip_data)

            # Verify data alignment bug
            valid = True
            for i in range(1016, file_size, 1024):
                if zip_data[i:i+1] in invalid_bytes:
                    valid = False
                    break

            if valid:
                return zip_data
            dummy_retries += 1

        return zip_data  # Fallback even if invalid

    def _parse_input(self, inp):
        try:
            parsed = IncomingStruct.parse(bytes(inp))
            data = parsed['data']
            if not data:
                return None
            if parsed['command_protocol'] == CommandProtocol.IN_BUTTON.value:
                self._last_action_time = time.time()
                return ButtonAction(index=data['index'], pressed=data['pressed'], state=data['state'])
        except:
            return None

    def set_small_window_mode(self, mode):
        try:
            self._small_window_mode = SmallWindowMode(mode)
        except:
            self._small_window_mode = SmallWindowMode.CLOCK

    def restore_small_window(self):
        self.set_small_window_data({'mode': self._small_window_mode})

    def _write_packet(self, packet):
        super()._write_packet(packet)
