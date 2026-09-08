# samples/

The runner scripts (`test_onboarding.ps1`, `kyc_test.py`) look here by default:

| File | What it is |
|------|------------|
| `document.png` | Identity document image — a passport (ICAO 9303 TD3) or a national ID card (TD1). PNG, JPEG, BMP or WebP. PDF is not supported. |
| `video.mp4` | A short clip of the same person: **at least ~5 s at 24 fps or better, 640×480 or larger**, frontal face, with an audio track. |

Both are empty in this repository by design.

## Why nothing is committed here

The evaluation in the thesis used the author's own Portuguese Cartão de Cidadão
and selfie recordings, plus those of one other consenting subject. That is
biometric and identity data, so it is not published — committing it would be
hard to reconcile with the data-protection argument the system is built to
support.

This means the reported validation runs cannot be reproduced byte-for-byte from
this repository alone. They can be reproduced in structure: supply any identity
document and a matching video clip, and the pipeline exercises the same code
paths and emits the same on-chain events.

## Requirements that matter

The liveness stage is rPPG-POS, which recovers a pulse from skin-colour
variation across frames. It needs **real temporal data** — a still image, a
too-short clip, or a video with no detectable frontal face will be rejected
rather than scored. The session-quality gate enforces the frame rate,
resolution, duration and luminance bounds listed in
`ai_verification/session_quality.py`.

The name passed via `-FullName` / `--name` is matched against the document, so
it has to be the name printed on whatever you put in `document.png`.

## Using your own paths instead

Rather than editing the committed scripts, copy them:

```
cp test_onboarding.ps1 test_onboarding.local.ps1     # PowerShell runner
cp kyc_test.py         kyc_test.local.py             # Python runner
```

Both `*.local.*` names are git-ignored, so local paths and personal details
stay out of the repository.
