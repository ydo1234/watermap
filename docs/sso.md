# Connexion commune Dolisi (OIDC)

L'activation est facultative : `OIDC_ENABLED=false` conserve la connexion Watermap actuelle. Le client utilise Authlib, le flux Authorization Code, PKCE S256 et un nonce. L'issuer doit être exactement celui publié par le fournisseur, y compris sa barre finale. Les URLs de retour sont fixes ; aucune URL de redirection n'est acceptée depuis le visiteur.

Paramètres serveur privés à définir avant démarrage :

```dotenv
OIDC_ENABLED=true
OIDC_ISSUER=https://auth-stag.dolisi.fr/auth/v1/
OIDC_CLIENT_ID=watermap-stag
OIDC_CLIENT_SECRET=secret_du_client
```

Le `WATERMAP_PUBLIC_URL` de recette reste `https://stag.watermap.dolisi.fr`. Le fournisseur doit enregistrer `/auth/callback`, `/auth/backchannel-logout` et l'URL de retour après déconnexion `/`, sous ce domaine exact. Les clients de production utilisent d'autres identifiants et secrets.

Les nouveaux comptes sont créés dans le fournisseur d'identité, avec confirmation d'e-mail et notification à l'administrateur. Le premier accès crée un utilisateur Watermap local sans droits administrateur. Les mots de passe et la récupération sont ensuite gérés par le fournisseur.

Pour un compte Watermap existant portant le même e-mail, le premier accès demande son ancien mot de passe sur `/auth/link`. Cette preuve est requise même si le fournisseur affirme l'e-mail vérifié. Le rattachement conserve l'identifiant numérique, les cartes, les fichiers GPX et `AdminAccess`. Aucun utilisateur n'est fusionné automatiquement par e-mail. Si l'ancien mot de passe est perdu, une récupération du compte existant ou une procédure administrateur vérifiée est nécessaire avant rattachement ; ne pas modifier directement les identifiants de propriétaires.

Les tables ajoutées sont `sso_identity`, `sso_session`, `sso_logout`. Le navigateur garde uniquement les identifiants de session signés ; les jetons OIDC restent dans la base privée côté serveur. Une session locale expire au plus tard après 12 h. Les notifications de déconnexion doivent être signées par le fournisseur, viser ce client et être récentes. Elles suppriment les sessions correspondantes ; les rejeux sont neutralisés. Les routes locales de mot de passe et les anciens cookies seuls ne permettent pas de contourner le SSO quand il est activé. Le POST de déconnexion conserve la protection CSRF.

Avant mise à jour de recette : sauvegarder SQLite avec l'API `backup`, les fichiers utilisateurs, l'environnement et l'image précédente. Ne pas recopier les secrets ou la base de production vers le fournisseur de recette. Tous les courriels de recette doivent rester redirigés vers `ydolisi@gmail.com`, avec le préfixe `[STAG]`, y compris les courriels du fournisseur d'identité.

Le retour arrière consiste à remettre l'image et l'environnement précédents (`OIDC_ENABLED=false` ou absent). Les tables ajoutées peuvent rester présentes. Ne pas restaurer une ancienne base à la place de la base vivante après de nouvelles écritures : conserver les nouveaux comptes et fichiers, ou effectuer une restauration sélective. Les nouveaux utilisateurs SSO n'ont pas de mot de passe Watermap connu ; leur accès après retour au mode local demande une récupération de mot de passe.
