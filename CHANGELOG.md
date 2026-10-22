# Változások / Changelog

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
