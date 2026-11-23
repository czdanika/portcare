# Változások / Changelog

## Kiadatlan / Unreleased

- **Javítás:** önálló konténer frissítésekor a Portainer „Recreate” API-ja nem vitte át a névtelen volume-okat (image `VOLUME`), így minden frissítés új volume-ot hozott létre, a régi árván maradt (pl. `nextcloud:latest` frissítésenként ~860 MB). Ha a konténernek van névtelen volume-ja, a PortCare mostantól a kézi újralétrehozást használja, ami visszacsatolja őket.
- **Fix:** Portainer's Recreate API dropped anonymous volumes when updating standalone containers, leaking one orphaned volume per update. Containers with anonymous volumes now use the manual recreate, which re-attaches them.

## v1.1.0 – 2026-10-28

- **Azonnali frissítés**: új szabály stackekre és konténerekre („Azonnal frissül”), illetve alapértelmezett ütemezésként („Azonnal, amint van új build”). Minden ellenőrzés után rögtön frissíti, amihez ugyanazzal a taggel új build jelent meg. Új verzióra (más tag) magától nem vált.
- Egy sikertelen azonnali frissítést nem próbál újra ugyanarra a buildre, csak ha újabb build jön.
- **Instant updates**: new rule for stacks / containers and as the default schedule; updates a new build of the same tag right after the check that found it.

## v1.0.0 – 2026-10-22

Az első önálló kiadás. / First standalone release.

**Felület**
- Magyar és angol nyelv, világos/sötét téma, Portainer-szerű kinézet, mobilnézet
- Telefonos app (PWA) iPhone-ra és Androidra, push értesítés (új frissítés, kész futás, hiba)
- Áttekintő: legutóbbi frissítések, utoljára ellenőrizve / utolsó futás
- Előzmények: „Mi történt” összefoglaló minden futásnál (frissült, hiba + tipp, nem volt új image), a nyers napló lenyitható
- „Újdonságok” link a projekt kiadási jegyzeteihez (OCI címke vagy registry alapján)
- A listák frissítés után maguktól frissülnek

**Frissítés**
- Stackenkénti szabály: alapértelmezett, csak jelez, saját ütemezés, kihagyva
- Önálló (stacken kívüli) konténerek ellenőrzése és frissítése, visszaállítással hiba esetén
- Csak a ténylegesen új image számít frissítésnek (image-összevetés előtte/utána)
- Verziókövetés: azonos pontosság és változat (pl. `-alpine`), főverzió-váltás külön jelölve
- Önfrissítés a Beállításokból

**Biztonság**
- Kötelező bejelentkezés, PBKDF2 jelszó-hash, brute-force védelem
- Titkosított API kulcs és webhook (Fernet), külön kötetben tárolt kulccsal
- TLS-tanúsítvány rögzítése (pinning) saját aláírású Portainerhez
- CSRF-védelem, Cloudflare Tunnel / reverse proxy mögött is

Eredet / Origin: [joshmadison/Portainer-Update-Service](https://github.com/joshmadison/Portainer-Update-Service) (MIT).
