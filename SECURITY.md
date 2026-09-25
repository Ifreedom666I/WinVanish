# Sicherheit & Signatur

## Ist WinVanish sicher?

Ja. WinVanish ist vollständig quelloffen (MIT-Lizenz) – der komplette Code liegt in
[`src/winvanish.py`](src/winvanish.py) und kann von jedem eingesehen und selbst zu einer `.exe`
gebaut werden (siehe [README](README.md#-selbst-bauen)). Die App:

- macht **keine Netzwerkverbindungen** und sendet **keinerlei Daten** nach außen
- speichert Einstellungen ausschließlich lokal in `config.json`
- nutzt Standard-Windows-APIs (`ShowWindow`, `SetForegroundWindow`, System-Mute) – nichts
  Ungewöhnliches, nichts, was tiefer als eine normale Desktop-App in dein System eingreift
- nutzt die `keyboard`-Bibliothek für den globalen Hotkey, weshalb manche Antiviren-Programme
  bei **jeder** selbst gebauten `.exe` mit Tastatur-Hook einen harmlosen Fehlalarm auslösen können

## Digitale Signatur – aktueller Stand

Die Releases werden mit einem **Kotsch.Tech-Zertifikat signiert** (sichtbar unter
*Rechtsklick auf die exe → Eigenschaften → Digitale Signaturen*), damit du nachvollziehen
kannst, dass eine Datei tatsächlich von Kotsch.Tech stammt und seit der Signierung nicht
verändert wurde.

**Ehrlich gesagt:** Für die ersten Releases ist das Zertifikat ein *self-signed*-Zertifikat
(„CN=Kotsch.Tech“). Das bedeutet:

- ✅ Die Signatur beweist Integrität (Datei wurde nicht nachträglich manipuliert) und zeigt
  „Kotsch.Tech“ als Signaturnamen an
- ❌ Windows SmartScreen vertraut diesem Zertifikat **noch nicht automatisch**, weil es nicht
  von einer offiziellen Zertifizierungsstelle (CA) ausgestellt wurde – du siehst also
  möglicherweise trotzdem eine SmartScreen-Warnung beim ersten Start

Ein Zertifikat, das SmartScreen sofort und ohne Warnung akzeptiert (EV-/OV-Code-Signing-Zertifikat
einer CA wie DigiCert, SSL.com oder Sectigo), kostet Geld und erfordert eine echte
Identitäts-/Firmenprüfung von Kotsch.Tech durch die Zertifizierungsstelle – das kann nur der
Inhaber von Kotsch.Tech selbst beantragen und bezahlen. Sobald ein solches Zertifikat vorliegt,
ist der Umstieg unkompliziert:

```powershell
signtool sign /f "kotsch-tech.pfx" /p "<passwort>" /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 WinVanish.exe
signtool sign /f "kotsch-tech.pfx" /p "<passwort>" /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 installer\Output\WinVanish-Setup.exe
```

## SmartScreen-Warnung bekommen? So gehst du sicher

1. Lade WinVanish **nur** von der offiziellen Quelle: die
   [Downloadseite auf kotsch.tech](https://kotsch.tech)
2. Falls SmartScreen warnt: **„Weitere Informationen“** → prüfe, dass als Herausgeber
   „Kotsch.Tech“ angezeigt wird → **„Trotzdem ausführen“**
3. Bei Zweifeln: Quellcode selbst lesen ([`src/winvanish.py`](src/winvanish.py), keine 400 Zeilen) oder
   die `.exe` bei [VirusTotal](https://www.virustotal.com/) hochladen

## Verantwortliche Offenlegung

Sicherheitslücke gefunden? Bitte **nicht** als öffentliches Issue posten, sondern direkt an
Kotsch.Tech melden: siehe Kontaktangaben auf [kotsch.tech](https://kotsch.tech).
