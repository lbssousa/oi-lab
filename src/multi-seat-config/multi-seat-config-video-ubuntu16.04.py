#!/usr/bin/python3
# -*- coding: utf-8 -*-

# Dependencies for Ubuntu 16.04
# - python3-dbus
# - python3-pyudev
# - python3-systemd

import re
import os
import sys
from subprocess import run

# Logging modules
import logging
from systemd.journal import JournalHandler

# Udev device handling modules
import pyudev

# DBus modules (for communication with systemd-logind)
import dbus

MAX_SEAT_COUNT = 5
XORG_CONF_DIR = '/etc/X11/xorg.conf.d'
LIGHTDM_CONF_DIR = '/etc/lightdm/lightdm.conf.d'
LOGIND_PATH = 'org.freedesktop.login1'
LOGIND_OBJECT = '/org/freedesktop/login1'
LOGIND_INTERFACE = 'org.freedesktop.login1.Manager'
SYSTEMD_PATH = 'org.freedesktop.systemd1'
SYSTEMD_OBJECT = '/org/freedesktop/systemd1'
SYSTEMD_INTERFACE = 'org.freedesktop.systemd1.Manager'

bus = dbus.SystemBus()
logind = dbus.Interface(bus.get_object(LOGIND_PATH, LOGIND_OBJECT),
                        dbus_interface=LOGIND_INTERFACE)
systemd = dbus.Interface(bus.get_object(SYSTEMD_PATH, SYSTEMD_OBJECT),
                         dbus_interface=SYSTEMD_INTERFACE)

logger = logging.getLogger(sys.argv[0])
logger.setLevel(logging.INFO)
logger.propagate = False
stdout_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    '%(asctime)s %(name)s[%(process)s] %(levelname)s %(message)s')
stdout_handler.setFormatter(formatter)
logger.addHandler(stdout_handler)
logger.addHandler(JournalHandler())


def ensure_open(file_path, mode):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    return open(file_path, mode)


def create_file(file_path, new_data):
    try:
        with ensure_open(file_path, 'r') as read_file:
            read_file.read()
    except FileNotFoundError:
        with ensure_open(file_path, 'w+') as new_file:
            new_file.write(new_data)


def update_file(file_path, new_data):
    try:
        with ensure_open(file_path, 'r') as read_file:
            old_data = read_file.read()

        if new_data != old_data:
            with ensure_open(file_path, 'w') as write_file:
                write_file.write(new_data)
    except FileNotFoundError:
        with ensure_open(file_path, 'w+') as new_file:
            new_file.write(new_data)


class SeatDevice:
    def __init__(self, device):
        self.device_path = device.device_path
        self.device_node = device.device_node
        self.sys_path = device.sys_path
        self.sys_name = device.sys_name
        self.seat_name = device.properties.get('ID_SEAT')

        try:
            self.is_auto_seat = device.properties.asbool('ID_AUTOSEAT')
        except:
            self.is_auto_seat = False

        parent = device.find_parent('pci')
        self.pci_slot = (parent.properties['PCI_SLOT_NAME'].lstrip('0000:')
                         if parent is not None else None)

    def attach_to_seat(self, seat_index):
        # Skip explicit seat attaching via systemd-logind if device has
        # udev property ENV{ID_AUTOSEAT} set to "1" (in this case,
        # it should already have a proper seat name).
        if not self.is_auto_seat or self.seat_name is None:
            try:
                seat_name = 'seat-{}'.format(seat_index)
                logind.AttachDevice(seat_name, self.sys_path, False)
                self.seat_name = seat_name

                # Sometimes the new udev rules are not automatically loaded
                # after calling systemd-logind's AttachDevice() method,
                # so we'll force it here.
                run(['udevadm', 'control', '--reload-rules'])
                run(['udevadm', 'trigger'])

                logger.info('Device %s successfully attached to seat %s',
                            self.sys_path, seat_name)
            except Exception as error:
                logger.error('Failed to attach device %s to seat %s!',
                             self.sys_path, seat_name)
                logger.error(error)


class SeatMasterDevice(SeatDevice):
    def __init__(self, device):
        super().__init__(device)

    def write_sample_lightdm_conf(self, seat_index):
        if self.seat_name is not None:
            file_path = '{}/70-oi-lab-{}.conf'.format(LIGHTDM_CONF_DIR,
                                                      self.seat_name)
            sample_data = """\
[Seat:{}]
autologin-user-timeout=20
#autologin-user=aluno{}
""".format(self.seat_name, seat_index + 1)
            create_file(file_path, sample_data)

    def attach_to_seat(self, seat_index):
        super().attach_to_seat(seat_index)
        self.write_sample_lightdm_conf(seat_index)


class SeatKMSVideoDevice(SeatMasterDevice):
    def __init__(self, fb, drm):
        super().__init__(fb)
        self.drm = [SeatDevice(d) for d in drm]

    def attach_to_seat(self, seat_index):
        # Attach the framebuffer device node
        super().attach_to_seat(seat_index)

        for node in self.drm:
            # Attach all other DRM device nodes as well
            node.attach_to_seat(self.seat_name)


class SeatSM501VideoDevice(SeatMasterDevice):
    def __init__(self, device):
        def pci_format(pci_slot, delimiter=''):
            return re.sub(r'\.|:', delimiter, pci_slot)

        super().__init__(device)
        self.output = device.properties.get('SM501_OUTPUT')
        self.display_number = int(pci_format(self.pci_slot), base=16)
        seat_address = pci_format(self.pci_slot, '-')
        xorg_address = pci_format(self.pci_slot, ':')
        file_path = '{}/21-oi-lab-sm501-{}.conf'.format(XORG_CONF_DIR,
                                                        seat_address)
        new_config_data = """\
Section "Device"
    MatchSeat "__fake-seat-{display_number}__"
    Identifier "Silicon Motion SM501 Video Card {pci_slot}"
    BusID "PCI:{xorg_address}"
    Driver "siliconmotion"
    Option "PanelSize" "1360x768"
    Option "Dualhead" "true"
    Option "monitor-LVDS" "Left Monitor"
    Option "monitor-VGA" "Right Monitor"
EndSection

Section "Screen"
    MatchSeat "__fake-seat-{display_number}__"
    Identifier "Silicon Motion SM501 Screen {pci_slot}"
    Device "Silicon Motion SM501 Video Card {pci_slot}"
    DefaultDepth 16
EndSection
""".format(display_number=self.display_number,
           pci_slot=self.pci_slot,
           xorg_address=xorg_address)
        update_file(file_path, new_config_data)

        # Enable permanently this socket unit, since it will be needed
        # even after multi-seat is configured.
        socket_unit = 'oi-lab-xorg-daemon@{}.socket'.format(
            self.display_number)
        systemd.EnableUnitFiles([socket_unit], False, True)

    def write_nested_xorg_conf(self):
        if self.seat_name is not None:
            file_path = '{}/22-oi-lab-nested-{}.conf'.format(XORG_CONF_DIR,
                                                             self.seat_name)
            new_config_data = """\
Section "Device"
    MatchSeat "{seat_name}"
    Identifier "Nested Device {pci_slot}"
    Driver "nested"
    Option "Display" ":{display_number}"
EndSection

Section "Screen"
    MatchSeat "{seat_name}"
    Identifier "Nested Screen {output} {pci_slot}"
    Device "Nested Device {pci_slot}"
    DefaultDepth 16
    Option "Output" "{output}"
EndSection
""".format(seat_name=self.seat_name,
                pci_slot=self.pci_slot,
                display_number=self.display_number,
                output=self.output)
            update_file(file_path, new_config_data)

    def attach_to_seat(self, seat_index):
        super().attach_to_seat(seat_index)
        self.write_nested_xorg_conf()


def scan_kms_video_devices(context):
    drms = context.list_devices(subsystem='drm')
    fbs = context.list_devices(subsystem='graphics')
    devices = [(fb,
                [drm
                 for drm in drms
                 if drm.parent == fb.parent and drm.device_node is not None])
               for fb in fbs
               if fb.device_node is not None]
    return [SeatKMSVideoDevice(*device) for device in devices]


def scan_sm501_video_devices(context):
    devices = context.list_devices(subsystem='platform', tag='master-of-seat')
    return [SeatSM501VideoDevice(device) for device in devices]


def main():
    context = pyudev.Context()
    kms_video_devices = scan_kms_video_devices(context)

    # In some cases (e.g. a VirtualBox VM without guest additions installed),
    # kms_video_devices may be empty here.
    if kms_video_devices:
        for device in kms_video_devices:
            logger.info('KMS video detected: %s -> %s',
                        device.device_node, device.sys_path)

            for drm in device.drm:
                logger.info('>>> DRM node detected: %s -> %s',
                            drm.device_node, drm.sys_path)

        # The first KMS/DRM video device (normally /dev/fb0) will be
        # reserved for seat0. There's no need to attach it explicitly,
        # but it's convenient to write a sample lightdm.conf for it.
        kms_video_devices[0].seat_name = 'seat0'
        kms_video_devices[0].write_sample_lightdm_conf(0)

        sm501_video_devices = scan_sm501_video_devices(context)

        for device in sm501_video_devices:
            logger.info('SM501 video detected: %s', device.sys_path)

        video_devices = kms_video_devices + sm501_video_devices

        # The total number of configrable seats is limited by
        # the availability of video devices, excluding /dev/fb0
        # (we'll reserve it for seat0).
        num_configurable_seats = min(MAX_SEAT_COUNT, len(video_devices)) - 1

        if num_configurable_seats > 0:
            for (index, video_device) in enumerate(video_devices[1:]):
                video_device.attach_to_seat(index + 1)


if __name__ == '__main__':
    main()
