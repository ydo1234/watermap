# Recette IPv6 sur le RPi

Windows résout `rpi4ng` vers les adresses IPv6 Ethernet et Wi-Fi avant les IPv4. Le port 8081 doit donc être publié dans les deux familles. Le Compose racine utilise `WATERMAP_DEV_BIND` pour IPv4 et `WATERMAP_DEV_BIND_V6` pour IPv6. Ce dernier reste sur `::1` par défaut ; sur le RPi de Yann, il vaut `::` après installation du filtre ci-dessous.

Les IPv6 LAN du RPi sont publiques. Le filtre `stag-ipv6.py` restreint uniquement l'accès IPv6 au port 8081 aux clients du LAN Ethernet/Wi-Fi, à Tailscale et à la boucle locale. Il ajoute sa propre table nftables `ip6 watermap_stag` avant le DNAT Docker, sans remplacer les tables existantes. Les préfixes sont calculés à partir des adresses actuelles de `eth0` et `wlan0`. Un timer les rafraîchit toutes les deux minutes, y compris après changement du préfixe fourni par le routeur.

Installer le filtre **avant** de publier le port IPv6 sur `::`. Les chemins sont ceux de cette installation. Depuis la racine du dépôt sur `stag` :

```bash
sudo install -d -m 755 /usr/local/libexec
sudo install -m 755 deploy/rpi/stag-ipv6.py /usr/local/libexec/watermap-stag-ipv6.py
sudo install -m 644 deploy/rpi/watermap-stag-ipv6.service /etc/systemd/system/watermap-stag-ipv6.service
sudo install -m 644 deploy/rpi/watermap-stag-ipv6.timer /etc/systemd/system/watermap-stag-ipv6.timer
sudo systemctl daemon-reload
sudo systemctl enable --now watermap-stag-ipv6.service watermap-stag-ipv6.timer
sudo nft list table ip6 watermap_stag
# Renseigner ensuite WATERMAP_DEV_BIND_V6=:: dans le .env privé, puis :
docker compose config --quiet
docker compose up -d --no-deps --wait --wait-timeout 90 watermap_dev
```

Conserver `WATERMAP_DEV_PUBLIC_URL=http://rpi4ng:8081`. La recette reste accessible par IPv4. Pour revenir à un accès uniquement local en IPv6, remettre `WATERMAP_DEV_BIND_V6=::1` et recréer le service Compose ; pour ne conserver que les accès locaux, remettre aussi `WATERMAP_DEV_BIND=127.0.0.1`.

Le filtre reste chargé si le service systemd est arrêté. Pour le retirer, arrêter d'abord la recette ou la limiter à la boucle locale, désactiver le service et son timer, puis supprimer uniquement la table `ip6 watermap_stag`. Ne pas charger `/etc/nftables.conf` avec son `flush ruleset`, ce qui effacerait les règles Docker/Tailscale.
