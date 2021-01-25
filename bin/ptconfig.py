#!/usr/bin/env python3
import os
import argparse

PT_CONFIG_PATH="/sys/bus/event_source/devices/intel_pt/format"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A helper to decode the intel_pt config of perf cmdline")
    parser.add_argument("--hex-config", type=str, default="", help="The hex version of config")
    parser.add_argument("--config", type=str, default="", help="The text version of config")
    arg = parser.parse_args()
    if len(arg.hex_config) == 0 and len(arg.config) == 0:
        print("Valid config not found")
        sys.exit(0)

    # option name(str) -> (start bit, end bit)
    pt_options = {}
    for opt in os.listdir(PT_CONFIG_PATH):
        with open(os.path.join(PT_CONFIG_PATH,opt), "r") as f:
            # conf should be something like "config:0"
            conf = f.read().split(':')
            bit_loc = conf[-1]
            if '-' in bit_loc:
                begin, end = bit_loc.split('-')
                pt_options[opt] = (int(begin), int(end))
            else:
                loc = int(bit_loc)
                pt_options[opt] = (loc, loc)
    ordered_pt_options = sorted(pt_options.items(), key=lambda x: x[1])
    hex_config = None
    text_config = None
    if len(arg.hex_config) > 0:
        # convert hex to text
        hex_config = arg.hex_config
        # [:1:-1] means removing the "0b" prefix then reverse the string, so that the index 0 is bit 0
        bits = bin(int(hex_config, base=16))[:1:-1]
        vals=[]
        for opt, loc in ordered_pt_options:
            val = int('0b'+bits[loc[0]:loc[1]+1], base=2)
            vals.append(opt + '=' + str(val))
        text_config = ','.join(vals)
    elif len(arg.config) > 0:
        # convert text to hex
        text_config = arg.config
        bits = 0
        for entry in text_config.split(','):
            if '=' in entry:
                opt, val = entry.split('=')
            else:
                opt = entry
                val = 1
            loc = pt_options[opt]
            val = int(val)
            bits += val << loc[0]
        hex_config = hex(bits)

    print("hex_config: %s" % hex_config)
    print("text_config: %s" % text_config)
