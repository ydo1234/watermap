#!/usr/bin/env python3
"""Restrict published IPv6 port 8081 to this RPi's LAN and Tailscale peers."""
import argparse
import ipaddress
import json
# Fixed argument lists only; IP prefixes are parsed by ipaddress before nft input.
import subprocess  # nosec B404

LAN_INTERFACES = frozenset(('eth0', 'wlan0'))
TABLE = 'watermap_stag'


def lan_prefixes(addresses):
    networks = {ipaddress.ip_network('fe80::/10')}
    for interface in addresses:
        if interface.get('ifname') not in LAN_INTERFACES:
            continue
        for address in interface.get('addr_info', []):
            if address.get('family') != 'inet6' or address.get('scope') != 'global':
                continue
            network = ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False)
            if network.version != 6 or network.prefixlen < 64:
                raise ValueError('Unexpectedly broad LAN prefix; refusing to expose IPv6 acceptance testing')
            networks.add(network)
    return tuple(str(network) for network in ipaddress.collapse_addresses(sorted(networks)))


def ruleset(prefixes, replace=False):
    prefix_list = ', '.join(prefixes)
    delete = f'delete table ip6 {TABLE}\n' if replace else ''
    # Match before Docker DNAT. Accept in this table leaves other firewall rules
    # active. Only disallowed IPv6 traffic to this host's port 8081 is dropped.
    return delete + f'''table ip6 {TABLE} {{
    set lan_prefixes {{
        type ipv6_addr
        flags interval
        elements = {{ {prefix_list} }}
    }}
    chain ingress {{
        type filter hook prerouting priority -110; policy accept;
        fib daddr type local tcp dport 8081 jump private_clients
    }}
    chain private_clients {{
        iifname "lo" counter accept
        iifname "tailscale0" counter accept
        iifname {{ "eth0", "wlan0" }} ip6 saddr @lan_prefixes counter accept
        counter drop
    }}
}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate without changing the firewall')
    args = parser.parse_args()
    addresses = json.loads(subprocess.check_output(['/usr/sbin/ip', '-j', '-6', 'address', 'show'], text=True, timeout=10))  # nosec B603
    prefixes = lan_prefixes(addresses)
    existing = subprocess.run(['/usr/sbin/nft', 'list', 'table', 'ip6', TABLE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0  # nosec B603
    rules = ruleset(prefixes, replace=existing)
    subprocess.run(['/usr/sbin/nft', '--check', '-f', '-'], input=rules, text=True, check=True, timeout=10)  # nosec B603
    if not args.check:
        subprocess.run(['/usr/sbin/nft', '-f', '-'], input=rules, text=True, check=True, timeout=10)  # nosec B603
    print(json.dumps({'port': 8081, 'ipv6_lan_prefixes': prefixes, 'applied': not args.check}))


if __name__ == '__main__':
    main()
