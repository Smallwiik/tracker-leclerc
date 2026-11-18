#!/usr/bin/env python3
"""
Tracker de stock / prix E.Leclerc
---------------------------------
Vérifie régulièrement des pages produit e.leclerc et t'envoie une notification
sur ton téléphone (via ntfy) et/ou Discord quand le statut change
(ex : "Indisponible" -> "Disponible") ou quand le prix change.

Installation :
    pip install requests

Utilisation :
    python leclerc_tracker.py --test     # vérifie une fois et affiche ce qui est détecté
    python leclerc_tracker.py            # tourne en boucle (Ctrl+C pour arrêter)

Notifications téléphone (gratuit, sans compte) :
    1. Installe l'app "ntfy" (Play Store)
    2. Abonne-toi à un sujet au nom unique, ex : kevin-pokebox-8f3k2
    3. Mets ce même nom dans NTFY_TOPIC ci-dessous
"""

import json
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

import requests

# ====================== CONFIG ======================

PRODUITS = [
    "https://www.e.leclerc/fp/pokemon-pokebox-mega-zeraora-ex-0196214141858",
    # ajoute d'autres liens ici
]

INTERVALLE_MINUTES = 5          # fréquence de vérification (évite < 2 min)
NTFY_TOPIC = ""                 # ex : "kevin-pokebox-8f3k2" (vide = désactivé)
DISCORD_WEBHOOK = ""            # URL de webhook Discord (vide = désactivé)

# Sur GitHub Actions, ces valeurs viennent des "secrets" du dépôt
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", NTFY_TOPIC)
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", DISCORD_WEBHOOK)

# Si un fichier produits.txt existe à côté du script (un lien par ligne), il remplace la liste
_fichier_produits = Path(__file__).with_name("produits.txt")
if _fichier_produits.exists():
    PRODUITS = [l.strip() for l in _fichier_produits.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]

# Mots qui indiquent une rupture de stock sur la page
MOTS_RUPTURE = ["indisponible", "rupture de stock", "épuisé", "plus disponible"]

# ====================================================

ETAT_FICHIER = Path(__file__).with_name("leclerc_etat.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def texte_visible(html):
    """Retire scripts/styles/balises pour garder le texte lisible de la page."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    # garde le texte alternatif des images (ex : logo "E.Leclerc" après "Vendu par")
    html = re.sub(r'(?is)<img[^>]*?\balt="([^"]*)"[^>]*>', r" \1 ", html)
    texte = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", texte)


def analyser(url):
    # Retire "?offerId=..." : sans ça, la page peut rester bloquée sur l'offre
    # d'un vendeur tiers au lieu d'afficher l'offre E.Leclerc
    url = url.split("?")[0]
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"            # sinon les accents sortent en "PokÃ©mon"
    html = r.text
    texte = texte_visible(html)
    bas = texte.lower()

    # Nom du produit
    m = re.search(r"(?is)<title>(.*?)</title>", html)
    nom = m.group(1).strip() if m else url

    # Prix : JSON-LD / meta si présent, sinon "Vendu 68.29€" ou "12,99 €" dans le texte
    prix = None
    for motif in (r'"price"\s*:\s*"?(\d+[.,]\d{2})',
                  r'itemprop="price"[^>]*content="(\d+[.,]\d{2})"',
                  r"Vendu\s+(\d{1,4}[.,]\d{2})\s?€",
                  r"(\d{1,4}[.,]\d{2})\s?€"):
        m = re.search(motif, html if "price" in motif else texte)
        if m:
            prix = m.group(1).replace(".", ",")
            break

    # Vendeur : la page affiche "Vendu par E.Leclerc" ou "Vendu par <vendeur tiers>"
    vendeur = "?"
    m = re.search(r"(?i)vendu par\b", texte)
    if m:
        apres = texte[m.end():m.end() + 80]
        if "e.leclerc" in apres.lower() or "lcommerce" in apres.lower():
            vendeur = "E.Leclerc"
        else:
            n = re.match(r"\s*([A-Za-zÀ-ÿ0-9][^€]{0,40}?)(?=\s+(?:SNC|Pays|Neuf|Occasion|Voir"
                         r"|En stock|Indisponible|Ajouter|\d)|\s*$)", apres)
            if n and not re.match(r"\d", n.group(1)):
                vendeur = n.group(1).strip()
    # Mention légale de l'offre E.Leclerc : "SNC LCOMMERCE"
    if vendeur == "?" and "lcommerce" in html.lower():
        vendeur = "E.Leclerc"
    # Vendeur introuvable -> on considère que c'est E.Leclerc (les vendeurs tiers
    # sont affichés en texte) : mieux vaut une notif de trop qu'un restock raté
    vendu_par_leclerc = (vendeur == "?"
                         or vendeur.lower().replace(" ", "").startswith("e.leclerc"))

    rupture = any(mot in bas for mot in MOTS_RUPTURE) and "en stock" not in bas
    if rupture:
        statut = "INDISPONIBLE"
    elif not vendu_par_leclerc:
        statut = "VENDEUR TIERS"      # dispo, mais pas vendu par E.Leclerc -> ignoré
    else:
        statut = "DISPONIBLE"
    return {"nom": nom, "prix": prix, "statut": statut, "vendeur": vendeur}


def notifier(titre, message, url):
    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": titre.encode("utf-8"), "Click": url,
                         "Priority": "high", "Tags": "shopping_cart"},
                timeout=10,
            )
        except Exception as e:
            log(f"Erreur ntfy : {e}")
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK,
                          json={"content": f"**{titre}**\n{message}\n{url}"},
                          timeout=10)
        except Exception as e:
            log(f"Erreur Discord : {e}")


def charger_etat():
    try:
        return json.loads(ETAT_FICHIER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sauver_etat(etat):
    ETAT_FICHIER.write_text(json.dumps(etat, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def verifier_tout(etat, test=False):
    for url in PRODUITS:
        try:
            info = analyser(url)
        except Exception as e:
            log(f"Échec pour {url} : {e}")
            continue

        log(f"{info['statut']:<13} | {info['prix'] or '?':>7} € | "
            f"vendu par {info['vendeur'][:20]:<20} | {info['nom'][:50]}")
        if test:
            continue

        avant = etat.get(url)
        if avant:
            # On ne notifie QUE pour les offres vendues par E.Leclerc
            devient_dispo = (info["statut"] == "DISPONIBLE"
                             and avant.get("statut") != "DISPONIBLE")
            prix_change = (info["statut"] == "DISPONIBLE" and info["prix"]
                           and avant.get("prix") and avant["prix"] != info["prix"])
            if devient_dispo:
                notifier("🟢 DISPO chez E.Leclerc ! " + info["nom"][:50],
                         f"Prix : {info['prix'] or '?'} €\nVendeur : {info['vendeur']}"
                         + (" (à vérifier)" if info["vendeur"] == "?" else ""), url)
                log("  → notification envoyée")
            elif prix_change:
                notifier("ℹ️ Prix modifié : " + info["nom"][:50],
                         f"{avant['prix']} € → {info['prix']} €", url)
                log("  → notification envoyée")
        etat[url] = info
        time.sleep(random.uniform(2, 5))   # petite pause entre produits
    if not test:
        sauver_etat(etat)


def main():
    test = "--test" in sys.argv
    if test:
        verifier_tout({}, test=True)
        if NTFY_TOPIC or DISCORD_WEBHOOK:
            notifier("Test tracker Leclerc", "Les notifications fonctionnent ✅",
                     PRODUITS[0])
            log("Notification de test envoyée")
        return

    if "--once" in sys.argv:            # mode GitHub Actions : une seule vérification
        verifier_tout(charger_etat())
        return

    log(f"Suivi de {len(PRODUITS)} produit(s), toutes les {INTERVALLE_MINUTES} min")
    etat = charger_etat()
    while True:
        verifier_tout(etat)
        # léger aléa pour ne pas taper à heure fixe
        time.sleep(INTERVALLE_MINUTES * 60 + random.randint(-30, 30))


if __name__ == "__main__":
    main()
