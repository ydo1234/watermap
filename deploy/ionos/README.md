# Déploiement IONOS

Watermap est servi sur https://watermap.dolisi.fr depuis `/opt/watermap` sur le VPS Debian 13 `82.165.183.204`. Le DNS du sous-domaine est géré chez OVH. Le code Python et les templates à déployer sont ceux à la racine du dépôt.

## Préparer une version

La branche `dev` reste sur le Raspberry Pi pour la recette. Après validation par Yann, fusionner `dev` dans `main`. Seule une version de `main` est déployée sur le VPS.

Depuis la racine du dépôt sur `main`, préparer un **nouveau** répertoire extérieur au dépôt :

```bash
python3 deploy/ionos/prepare-release.py /tmp/watermap-release
```

Le script rassemble les fichiers nécessaires et crée le dossier `source/` attendu par Compose. Il utilise `requirements.lock`, le Dockerfile de production et les images Python et Caddy figées par empreinte. `release.json` identifie le commit source et indique si des modifications locales étaient présentes.

Le répertoire généré ne contient ni secrets ni données utilisateur. Sur une première installation, renseigner `runtime.env` à partir de `runtime.env.example`, donner au fichier les permissions 600, puis créer `data/db`, `data/pages` et `data/uploads` avec l'UID/GID 10001. Pour une mise à jour, conserver le `runtime.env` et les données du VPS ; ne pas les remplacer par un ancien instantané du RPi.

`bootstrap-vps.sh` installe Docker depuis son dépôt officiel sur un nouveau VPS Debian Trixie amd64. Il crée `/opt/watermap` et `/var/backups/watermap`. Ce script d'installation n'est pas nécessaire sur le serveur existant.

## Construire et déployer

Transférer les fichiers préparés dans `/opt/watermap`, après avoir sauvegardé l'installation existante. Sur le VPS, toutes les commandes suivantes se lancent dans ce répertoire. Le test utilise des comptes et données temporaires et ne modifie pas les comptes réels :

```bash
cd /opt/watermap
docker compose config --quiet
docker compose build app
docker run --rm --read-only --tmpfs /tmp:size=768m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 -v /opt/watermap/smoke-test.py:/tmp/smoke-test.py:ro watermap-ionos:production python /tmp/smoke-test.py
docker compose up -d --wait --wait-timeout 90
docker compose ps
```

Le test `smoke-test.py` fonctionne indépendamment du nombre de comptes et de fichiers existants. Les routes d'inscription, connexion, CSRF, isolation des comptes, import, génération d'une carte, téléchargement, suppression et réinitialisation à usage unique sont vérifiées ; l'envoi de courriel y est simulé. L'espace temporaire de 768 Mo permet de tester le contrôle de réserve disque sans toucher aux volumes réels.

La clé `SECRET_KEY` doit comporter au moins 32 caractères aléatoires ; conserver la clé de production existante. La mise à jour invalide les anciennes sessions et les anciens liens de réinitialisation : les utilisateurs doivent se reconnecter. Le schéma des comptes et les mots de passe sont conservés ; un index SQLite sur les adresses email est ajouté. Les nouvelles cartes utilisent tous les segments du tracé. Les anciennes cartes HTML conservent leurs résultats historiques et peuvent être recréées depuis leurs GPX pour bénéficier du calcul corrigé.

`check-live.py` contrôle l'accès au compte propriétaire et à ses fichiers via HTTPS sans imprimer le cookie de session. `check-smtp.py` vérifie les identifiants SMTP et TLS sans envoyer de message. Ces deux contrôles nécessitent la configuration et les volumes réels : les exécuter avec `docker compose run --rm -T --no-deps -v /opt/watermap/check-live.py:/tmp/check-live.py:ro app python /tmp/check-live.py` (ou `check-smtp.py`). Ne pas utiliser le conteneur isolé sans volumes du test précédent pour ces contrôles.

Le conteneur applicatif utilise l'UID 10001, un système de fichiers protégé en écriture, un worker Gunicorn et quatre threads. Un seul worker conserve en mémoire le suivi des tâches GPX. Le socket de contrôle Gunicorn est désactivé. Les données sont montées depuis `data/` ; les secrets sont injectés au lancement et sont exclus de l'image.

Caddy publie les ports 80 et 443 et renouvelle le certificat HTTPS. Le port applicatif 8080 est lié à la boucle locale du VPS. Les cookies sécurisés et les en-têtes du proxy sont configurés dans `wsgi.py`. Le point de contrôle interne `/_health` n'est pas accessible publiquement.

## Sauvegardes

Sur le VPS, `backup.py` est installé dans `/opt/watermap`. Installer les unités `watermap-backup.service` et `watermap-backup.timer` dans `/etc/systemd/system`, puis activer le timer. Une sauvegarde cohérente est créée à 03:15 UTC chaque jour dans `/var/backups/watermap`, avec sept jours de rétention. Le conteneur est brièvement suspendu pour la copie, puis sa reprise saine est vérifiée. L'archive contient les données, le code, les références GPX, la configuration, les secrets et un manifeste d'intégrité. Les archives sont privées.

Sur le RPi, les scripts `pull-backup.py` et `check-site.py` sont installés dans `/home/yann/watermap-deployment`. `install-rpi-timers.sh` installe les unités associées. Les scripts et unités fournis correspondent aux chemins et à l'utilisateur de cette installation ; les adapter pour un autre poste d'administration.

Le RPi récupère la dernière archive à 04:00 UTC, vérifie son empreinte et celle de tous les fichiers de données, puis restaure temporairement SQLite et contrôle son intégrité et le nombre de comptes. Les copies sont gardées quatorze jours dans `vps-backups/`. Le contrôle HTTPS s'exécute toutes les cinq minutes et consigne ses résultats dans le journal systemd, sans notification externe.

Le site fonctionne indépendamment du RPi. La copie hors VPS et le contrôle de disponibilité nécessitent que le RPi soit allumé et connecté. Les timers persistants rattrapent une exécution manquée ; une longue indisponibilité du RPi peut dépasser la rétention des archives du VPS.

## Migration initiale et restauration

La base et le mot de passe du compte existant ont été conservés à la bascule du 3 novembre 2026. Les anciennes pages et GPX ont été rattachés à ce compte, avec adaptation des liens de téléchargement. Les scripts et instantanés de cette migration restent localement dans `/home/yann/watermap-deployment` ; les données privées ne sont pas versionnées.

Le conteneur d'origine du RPi `watermap-watermap_prod-1` est arrêté, sa politique de redémarrage est désactivée et ses volumes sont conservés. Après des écritures sur le VPS, récupérer les dernières données avant tout retour au RPi pour éviter leur perte.

Pour restaurer une archive, arrêter l'application, conserver une copie des données actuelles et préparer la restauration dans un dossier distinct. Vérifier le manifeste et SQLite, puis remettre en place les données, le code et la configuration archivés. Rétablir l'UID/GID 10001 sur les données, reconstruire l'image si nécessaire et redémarrer Compose. Contrôler ensuite connexion, carte, GPX et HTTPS. Les certificats peuvent être réémis si le domaine et les ports publics sont disponibles.
