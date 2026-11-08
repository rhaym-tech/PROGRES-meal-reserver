# PROGRES+ WebEtu meal reserver

Automates the official PROGRES WebEtu 2.4.0 meal-reservation flow: it logs in
with the student's own account, discovers the current academic year, DIA,
wilaya and residence, logs in to the meal API, and reserves breakfast, lunch
and dinner at a chosen restaurant for the next days.

The client mirrors the app's HTTP calls, including the HMAC signature headers
that the live meal API enforces:

```
X-Timestamp: <unix seconds>
X-Nonce:     <uuid4>
X-Signature: HMAC-SHA256(key, "<timestamp>|<nonce>|<data>")
```

`<data>` is the compact JSON request body for POST requests and an empty
string for GET requests.

## Requirements

- Python 3.9+
- `requests` (`python -m pip install requests`)

## Credentials

Credentials are never hardcoded. Use one of:

1. Environment variables (recommended):

   ```bat
   set WEBETU_USERNAME=my_student_id
   set WEBETU_PASSWORD=my_password
   set RESTO_DEPOT=123
   ```

2. A `.env` file next to the script (same `KEY=VALUE` names).

3. A JSON config file. Copy `progres_reserver.example.json` to
   `progres_reserver.json` (auto-loaded) and fill in your credentials, or pass
   `--config path\to\file.json`.

Do not commit `.env` or a config file containing your password.

## Usage

```bat
python progres_meal_reserver.py --list-depots
python progres_meal_reserver.py --list-reservations
python progres_meal_reserver.py --dry-run --depot 123
python progres_meal_reserver.py --depot 123
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--depot ID_OR_NAME` | restaurant id or (unique) name; required to reserve |
| `--offsets 1,2,3` | reservation day offsets, default tomorrow to +3 days |
| `--dates YYYY-MM-DD,...` | explicit dates instead of offsets |
| `--meals 1,2,3` | 1 = breakfast, 2 = lunch, 3 = dinner |
| `--dry-run` | print the plan without sending reservations |
| `--force` | reserve even if the same date/meal already exists |
| `--no-sign` | disable signatures (do not use against the live API) |
| `--no-log-file` | disable `progres_reserver.log` |
| `-v` | debug logging |

Reservations are sent as one `POST /reservemeal` per meal type, with one JSON
detail per date. The script never retries a reservation automatically, and it
skips date/meal pairs that the reservations endpoint already reports, so
repeated runs do not double-book.

Exit codes: `0` success (or dry run), `1` API/network error, `2` configuration
error or rejected meal type, `130` interrupted.

## Running every 3 days

With the default offsets (`1,2,3`) one run covers tomorrow through +3 days.
Running the script every 3 days therefore keeps coverage continuous. Create a
Windows Task Scheduler task once, from the account that owns the reservation:

```bat
schtasks /Create /TN "PROGRES+ meals" /SC DAILY /MO 3 /ST 00:10 ^
  /TR "\"Path\To\python.exe\" \"Path\To\progres_meal_reserver.py\" --depot [number]"
```

Check or adjust it with `taskschd.msc`. The task inherits the user's
environment variables; if you prefer a config file, add
`--config "D:\Path\To\progres_reserver.json"` to the command.

Or on Linux:
Open crontab
```bash
crontab -e
```
Then add:
```bash
10 0 */3 * * /usr/bin/python3 /path/to/progres_meal_reserver.py --depot [number]
```