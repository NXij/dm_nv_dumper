#!/usr/bin/python3
import sys
import usb.core
import usb.util
import struct
import json
import binascii
import csv
import subprocess
import re

dev_id = (0x04e8,0x6862) # DM usb device id
start_bits = (0xdc34,0xfe12) #0x34,0xdc,0x12,0xfe
counter = 0 # nv read loop counter

class DMCmdOut:
    START = 0
    STOP = 512
    REGISTRY_INFO_REQ = 32768 #0x8000 - without parameters OR len(values) + values (only returns items up to index 6823) OR count
    REGISTRY_READ_REQ = 33280  #0x8200 - without parameters OR b'x\01' + len(values) + values
    REGISTRY_WRITE_REQ = 33792 #0x8400

class DMCmdIn:
    REGISTRYINFOGET = 33024 # 0x8100 - Registry item info
    REGISTRYREAD = 33536 # 0x8300 - Registry item value payload

# Check nv count from device via adb
def get_nvcount() -> int:
    try:
        output = subprocess.check_output([
                    "adb", "shell", "su -c",
                    "'echo ATV0+GOOGGETNV?\r > /dev/umts_router & cat /dev/umts_router'"
                ])
    except:
        print("Failed to send an adb command!")
        exit()

    match = re.search(r"NV count=(\d+)", str(output))
    if match:
        nv_count = int(match.group(1))
    else:
        print(f"NV count not found. Try again...")
        exit()
    return nv_count

nvcount = get_nvcount()
element_list = [None] * nvcount # Initialize nv list

# Handle USB Comms
def open_dm(device: tuple):
    # Open DM usb on the device via adb
    if not usb.core.find(idVendor=device[0], idProduct=device[1]):
        print("DM device not found, trying to open DM via adb...")
        try:
            subprocess.run([
                "adb", "shell", "su -c",
                "'resetprop ro.bootmode usbradio && "
                "resetprop ro.build.type userdebug && "
                "stop DM-daemon && start DM-daemon && "
                "setprop sys.usb.config acm,dm,adb && "
                "setprop persist.vendor.usb.usbradio.config dm'"
            ])
        except:
            print("Failed to send an adb command!")
            exit()

    # Poll till devices becomes available
    while not usb.core.find(idVendor=device[0], idProduct=device[1]):
        pass

    dev = usb.core.find(idVendor=device[0], idProduct=device[1])

    # Get an endpoint instance
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]

    # Find the OUT endpoint
    ep_out = usb.util.find_descriptor(
        intf,
        # match the first OUT endpoint
        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)

    if ep_out is None:
        raise ValueError("Endpoint OUT not found")

    # Find the IN endpoint
    ep_in = usb.util.find_descriptor(
        intf,
        # match the first IN endpoint
        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

    if ep_in is None:
        raise ValueError("Endpoint IN not found")
    return ep_out, ep_in

ep_out, ep_in = open_dm(dev_id)

def send_dm_message(cmd,*args) -> None:
    # Prepare the header
    header = struct.pack('<BHBHHBHL',
        0x7f,           # startFlag
        0,              # length1 (placeholder)
        0,              # control
        0,              # length2 (placeholder)
        0,              # seq
        0xa0,           # main
        cmd,            # cmd
        0               # timestamp (using 1 as in the example)
    )

    # Add items to body
    body = b''.join(
        # Add byte else pack int in <H
        item if isinstance(item, bytes) else
        struct.pack('<H', item) if isinstance(item, int) else
        b''.join(
            # Add byte else pack int in <H from a list
            sub_item if isinstance(sub_item, bytes) else
            struct.pack('<H', sub_item)
            for sub_item in item
        )
        for item in args
    )

    # Update lengths
    total_length = len(header) + len(body) + 1  # +1 for footer

    length1 = total_length - 2
    length2 = length1 - 3

    # Update header with correct lengths
    header = header[:1] + struct.pack('<H', length1) + header[4:5] +struct.pack('<H', length2) + header[6:]
    message = header + body + b'\x7e'
    ep_out.write(message)

# Handle received Registry messages
def dm_message_id(dmBytes: bytes) -> int:
    # MessageId is located at the 10th and 11th bytes
    return struct.unpack_from('<H', dmBytes, 9)[0]

# Handle nv items
def populate_nv(dm_bytes: bytes, elementList: list[dict]) -> int:
    global counter
    message_id = dm_message_id(dm_bytes)
    packet = struct.unpack_from('<H', dm_bytes, 19)[0]
    # Skip packet header
    curr_pos = 21

    for chunk in range(packet):
        item_index = struct.unpack_from('<H', dm_bytes, curr_pos)[0]
        if item_index < len(elementList):
            chunk_size, chunk_count = struct.unpack_from('<II', dm_bytes, curr_pos + 2)
            curr_pos += 10
            count = chunk_size * chunk_count

            if element_list[item_index] is None:
                element_list[item_index] = {}
            element_list[item_index]['Index'] = item_index

            # Populate registry info fields
            if message_id == DMCmdIn.REGISTRYINFOGET:
                dmByte1 = dm_bytes[curr_pos]
                registry_name = dm_bytes[curr_pos + 1 : curr_pos + 1 + dmByte1].decode('ascii').strip()
                dmByte2 = dm_bytes[curr_pos + 1 + dmByte1]
                type_name = dm_bytes[curr_pos + 2 + dmByte1 : curr_pos + 2 + dmByte1 + dmByte2].decode('ascii').strip()
                curr_pos += 2 + dmByte1 + dmByte2

                elementList[item_index].update({
                    'RegistryName': registry_name,
                    'Size': chunk_size,
                    'Count': chunk_count,
                    'TypeName': type_name
                })
                counter += 1

            # Populate registry payload fields
            elif message_id == DMCmdIn.REGISTRYREAD:
                if 'Payload' not in elementList[item_index]:
                    count = chunk_size * chunk_count
                    elementList[item_index]['Payload'] = dm_bytes[curr_pos : curr_pos + count].hex(",")
                    curr_pos += count
                    counter += 1
    return counter

# Parse and handle packet type
def process_packet_type(hdlc_packet) -> bool:
    match (dm_message_id(hdlc_packet)):
        case DMCmdIn.REGISTRYINFOGET | DMCmdIn.REGISTRYREAD:
            item_count = populate_nv(hdlc_packet, element_list)
            print(f"\rDump status: {item_count // 2}/{nvcount}", end="")
            if item_count == nvcount:
                send_dm_message(DMCmdOut.REGISTRY_INFO_REQ)
            if item_count // 2 == nvcount:
                write_output(element_list)
            return True
        case _:
            return False

# Split buffer into packets
def process_buffer(buffer):
    start_index = 0
    is_handled = False
    while start_index < len(buffer):
        start_index = buffer.find(0x7F, start_index)

        if start_index == -1:
            break
        if start_index + 3 > len(buffer):  # Ensure there's enough data to read length
            break
        packet_length = (struct.unpack_from('<H',buffer, start_index + 1)[0])
        end_index = buffer.find(0x7E,start_index + packet_length + 1)

        if end_index != -1:
            is_handled = process_packet_type(buffer[start_index:end_index + 1])
            start_index = end_index + 1
        else:
            break

    # Remove processed data from the buffer
    return buffer[start_index:], is_handled

# Read usb and loop
def continuous_read() -> None:
    reg_read_index = 0
    is_handled = True
    buffer = bytearray()
    try:
        while True:
            reg_read_payload = list(range(reg_read_index,reg_read_index+40))

            if is_handled == True and reg_read_index < nvcount:
                send_dm_message(DMCmdOut.REGISTRY_READ_REQ,b'\x00',len(reg_read_payload),reg_read_payload)

            try:
                data_in = ep_in.read(2048)  # Brrrrr
                buffer.extend(data_in)
            except usb.core.USBError as e:
                if e.errno == 110:  # Timeout error
                    continue
                else:
                    print(f"USB Error: {e}")
                    break

            # Process buffer for complete packets
            buffer,is_handled = process_buffer(buffer)
            if is_handled == True and reg_read_index < nvcount:
                reg_read_index += 40

    except KeyboardInterrupt:
        send_dm_message(DMCmdOut.STOP)
        exit()

# Get baseband version and dump data
def write_output(element_list) -> None:
    send_dm_message(DMCmdOut.STOP)
    modem_fw_ver = subprocess.check_output(["adb", "shell","getprop ro.build.expect.baseband"])
    modem_fw_ver = '-'.join(modem_fw_ver.decode("utf-8").split('-')[:3])

    filename = (modem_fw_ver + '_full.json', modem_fw_ver + '_nv.csv')

    payload_filter_list = {
        "CAL.Common.Imei","DS_CAL.Common.Imei","CAL.Common.Imei_2nd",
        "PAL.Imsi","DS_PAL.Imsi","NASU.LAST.INSERTED.IMSI",
        "DS_NASU.LAST.INSERTED.IMSI","PAL.TCS.ImsiValue",
        "PAL.TCS_DS.ImsiValue","!SAEL3.ImsiValue",
        "!SAEL3_DS.ImsiValue","!NRMM.IMSI","ds_!NRMM.IMSI",
        "RM.IMSI","ds_RM.IMSI","PSS.AIMS.IMSI","DS.PSS.AIMS.IMSI",
        "CAL.LTE.Sim.T.Imsi"
    }

    print(f"\nStripping IMEI/IMSI from dump...")
    for i in element_list:
        if i['RegistryName'] in payload_filter_list:
            i['Payload'] = re.sub(r'\d', 'B', i['Payload'])

    with open(filename[0], 'w') as file:
        print(f"Writing {filename[0]}")
        json.dump(element_list, file,indent=4)

    csvheader = ['crc32', 'item']
    with open(filename[1], 'w', newline='') as csvfile:
        print(f"Writing {filename[1]}")
        nvcsv = csv.writer(csvfile, delimiter=',')
        nvcsv.writerow(csvheader)

        for i in element_list:
            if i not in [None]:
                nvcsv.writerow([binascii.crc32(i['RegistryName'].encode('utf8')), i['RegistryName']])
    exit()

# REGISTRY_INFO_REQ expects count of requested items as first parameter
# Not using any parameters will return all items (6823 items)
# for some reason DM doesn't know about items past that
# ShannonDM behaves the same, luckily the Pixel 9 series is excempt from this

# Argument values are packed as <H if integers, also <H if integers in a list
# To pass single bytes, pass them as bytes e.g. b'\x01'

def main() -> None:
    send_dm_message(DMCmdOut.STOP)
    send_dm_message(DMCmdOut.START,start_bits)
    continuous_read()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"An error occured: {e}")

    try:
        send_dm_message(DMCmdOut.STOP)
    except Exception:
        pass

    sys.exit()
