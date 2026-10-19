# Local ATVLoadly image: upstream v0.7.1 with plumesign patched to send the
# Windows iTunes X-Mme-Client-Info (backport of claration/impactor#290, Impactor v2.7.1),
# because Apple blocked the Pixel 7 client info on 2026-10-14 (atvloadly#215).
# Drop this and go back to bitxeno/atvloadly:<new tag> once upstream ships the fix.
FROM bitxeno/atvloadly:v0.7.1
COPY plumesign /usr/bin/plumesign
RUN chmod 755 /usr/bin/plumesign && /usr/bin/plumesign --version
