# DM Dumper
Dumps nv items from a Pixel via DM.

This is useful for research regarding the Shannon modem and it's features as it gets firmware updates.

This is ROOT only!
## What does it output?
It outputs two files in the root directory of the script:
  1) a csv file with the names and the CRC32 value of the name.
  2) a json file with the names, data types and payloads

## How do I run this?
This requires a rooted Pixel device on the Tensor SOC platform. Works the best with Pixel 9 series.

Run it with Python 3.

Tested on Linux but should work with WSL if you pass the usb device through to it.

Requires pyusb `pip install pyusb`

Run the script
`./nv_dump2.py`

## Is there a better way to view the json output?
I have written a little page for this. 

https://nxij.github.io/dm_nv_viewer/

There is an example json in this repository that you can load.
