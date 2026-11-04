import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('stag_ipv6', Path(__file__).resolve().parents[1]/'deploy/rpi/stag-ipv6.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_physical_lan_prefixes_are_allowed():
    def interface(name, address, prefix=64):
        return {'ifname':name,'addr_info':[{'family':'inet6','scope':'global','local':address,'prefixlen':prefix}]}
    prefixes = module.lan_prefixes([
        interface('eth0','2001:db8:1::1'), interface('wlan0','2001:db8:1::2'),
        interface('docker0','2001:db8:bad::1'), interface('tailscale0','fd7a::1',128),
    ])
    assert set(prefixes)=={'2001:db8:1::/64','fe80::/10'}
    rules=module.ruleset(prefixes,replace=True)
    assert rules.startswith('delete table ip6 watermap_stag\n')
    assert 'tcp dport 8081 jump private_clients' in rules
    assert 'iifname "tailscale0" counter accept' in rules
    assert 'counter drop' in rules
    assert 'flush ruleset' not in rules
    assert '::/0' not in rules


def test_missing_global_lan_stays_closed():
    assert module.lan_prefixes([])==('fe80::/10',)


def test_broad_lan_prefix_refused():
    with pytest.raises(ValueError,match='broad'):
        module.lan_prefixes([{'ifname':'eth0','addr_info':[{'family':'inet6','scope':'global','local':'2001:db8::1','prefixlen':32}]}])
