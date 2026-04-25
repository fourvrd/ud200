from typing import Union
import hid
from .device import DeckDevice
from .devices.ulanzi_d200 import UlanziD200Device

DEVICE_MAP = {
    (UlanziD200Device.USB_VENDOR_ID, UlanziD200Device.USB_PRODUCT_ID): UlanziD200Device,
}


def auto_connect() -> Union[None, DeckDevice]:
    for device_dict in hid.enumerate():
        tuple_id = (device_dict['vendor_id'], device_dict['product_id'])
        if tuple_id in DEVICE_MAP:
            device_class = DEVICE_MAP[tuple_id]

            if device_dict['interface_number'] != device_class.INTERFACE_NUMBER:
                continue

            try:
                device = hid.device()
                device.open_path(device_dict['path'])
                device.set_nonblocking(True)

                return device_class(device)

            except Exception as e:
                print(f"Błąd otwierania urządzenia: {e}")
                continue

    return None
