# spotter-naip-cutter
Cuts public-domain aerial imagery into PMTiles tile archives.

`cut_cell.py` takes one z12 cell, finds USDA NAIP imagery for it on Microsoft Planetary Computer, reads only the pixels it needs from the remote COGs, renders z12 to z17 as 512 px WebP tiles, writes one PMTiles v3 archive and uploads it to R2. It uses the newest year that covers the cell and adds older years only where the newest leaves a gap.

## Environment

| Variable | Use |
| --- | --- |
| `OD1_CELL` | Required. The cell as `12/x/y`. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` | Required unless `--dry-run` is set. |
| `RUNNER_TEMP` | Directory for the default error file. Falls back to the system temp directory. |

## Flags

- `--dry-run --out PATH` writes the archive to PATH and skips the upload. The two flags go together.
- `--err PATH` sets the error file. The default is a randomly named file. Tracebacks and warnings go only to this file, which can contain cell details, so treat it as private.

## Status

Stdout gets exactly one line and stderr gets nothing.

| Word | Exit | Meaning |
| --- | --- | --- |
| `ok` | 0 | Archive written or uploaded. |
| `none` | 0 | No imagery covers the cell, nothing written. |
| `fail:E1` | 1 | Bad input or flags. |
| `fail:E2` | 1 | STAC search failed. |
| `fail:E3` | 1 | Reading imagery failed. |
| `fail:E4` | 1 | Rendering or writing failed. |
| `fail:E5` | 1 | Upload failed or R2 environment incomplete. |

## Dispatch workflow

`.github/workflows/cut.yml` runs only on a `repository_dispatch` event of type `cut`, with an encrypted cell in `client_payload.c`. It calls `run_job.py`, which decrypts the cell with AES-256-GCM, tells the runner to mask every value derived from it, runs `cut_cell.py` and posts the status word to the callback URL. The run log shows only one fixed line such as `ok callback:ok`.

| Secret | Use |
| --- | --- |
| `OD1_CELL_KEY` | 32-byte AES key, standard base64. |
| `OD1_CALLBACK_URL`, `OD1_CALLBACK_TOKEN` | Status callback and its bearer token. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` | Upload target. |

The callback body is `{"c": "<ciphertext as received>", "status": "<word>"}`. The status word is one from the table above. A key or decryption failure reports `fail:E1` without running the cutter. The driver retries the callback twice on a network error or 5xx and never on 4xx. It exits 0 only when the status is `ok` or `none` and the callback succeeded.

## Tests

    pip install -r requirements.txt pytest
    pytest
