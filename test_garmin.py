import os
from datetime import date
from garminconnect import Garmin
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ.get("GARMIN_EMAIL", "")
PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

def main():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("GARMIN_EMAIL/GARMIN_PASSWORD fehlen in .env")
    print("Verbinde mit Garmin Connect...")
    client = Garmin(EMAIL, PASSWORD, prompt_mfa=lambda: input("Garmin MFA-Code eingeben: "))
    client.login()
    print("Login erfolgreich!\n")

    today = date.today().isoformat()

    # Basisdaten abrufen
    stats = client.get_stats(today)
    sleep = client.get_sleep_data(today)
    hrv = client.get_hrv_data(today)

    print(f"=== Garmin Basisdaten ({today}) ===\n")

    # Body Battery
    bb = stats.get("bodyBatteryMostRecentValue", "n/a")
    print(f"Body Battery:     {bb}")

    # Ruhepuls
    rhr = stats.get("restingHeartRate", "n/a")
    print(f"Ruhepuls:         {rhr} bpm")

    # Schlaf
    sleep_seconds = sleep.get("dailySleepDTO", {}).get("sleepTimeSeconds", 0)
    sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else "n/a"
    sleep_score = sleep.get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value", "n/a")
    print(f"Schlafdauer:      {sleep_hours} Stunden")
    print(f"Schlaf-Score:     {sleep_score}")

    # HRV
    hrv_value = None
    if hrv:
        hrv_value = hrv.get("hrvSummary", {}).get("lastNight", "n/a")
    print(f"HRV (letzte Nacht): {hrv_value}")

    print("\nDone.")

if __name__ == "__main__":
    main()
