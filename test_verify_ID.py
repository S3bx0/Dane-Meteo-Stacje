import unittest

import pandas as pd

from verify_ID import (
    resolve_country_codes,
    search_by_city,
    search_by_country,
    search_by_country_and_city,
    split_country_city_query,
)


class VerifyIDSearchTests(unittest.TestCase):
    def setUp(self):
        self.stations = pd.DataFrame(
            [
                {"ID": "GME00127438", "Name": "BERLIN TEMPELHOF", "Latitude": 52.47, "Longitude": 13.40},
                {"ID": "USC00200723", "Name": "BERLIN", "Latitude": 42.93, "Longitude": -82.91},
                {"ID": "PL000001", "Name": "WARSZAWA OKECIE", "Latitude": 52.16, "Longitude": 20.96},
                {"ID": "FR000001", "Name": "PARIS MONTSOURIS", "Latitude": 48.82, "Longitude": 2.33},
            ]
        )
        self.stations["NameNorm"] = self.stations["Name"].str.lower()

        self.countries = pd.DataFrame(
            [
                {"Code": "GM", "Country": "GERMANY", "CountryNorm": "germany"},
                {"Code": "PL", "Country": "POLAND", "CountryNorm": "poland"},
                {"Code": "FR", "Country": "FRANCE", "CountryNorm": "france"},
                {"Code": "US", "Country": "UNITED STATES", "CountryNorm": "united states"},
            ]
        )

    def test_split_country_city_query(self):
        country, city = split_country_city_query("DE, BERLIN")
        self.assertEqual(country, "DE")
        self.assertEqual(city, "BERLIN")

    def test_resolve_country_codes_supports_iso_mapping(self):
        codes = resolve_country_codes("DE", self.countries)
        self.assertIn("DE", codes)
        self.assertIn("GM", codes)

    def test_search_by_city_matches_typo(self):
        results = search_by_city(self.stations, "BERLN")
        self.assertFalse(results.empty)
        self.assertIn("GME00127438", results["ID"].values)

    def test_search_by_country_uses_mapped_code(self):
        results = search_by_country(self.stations, "DE", countries=self.countries)
        self.assertFalse(results.empty)
        self.assertTrue(all(station_id.startswith("GM") for station_id in results["ID"]))

    def test_search_by_country_and_city_filters_to_target_country(self):
        results = search_by_country_and_city(self.stations, self.countries, "DE", "BERLIN")
        self.assertFalse(results.empty)
        self.assertTrue(all(station_id.startswith("GM") for station_id in results["ID"]))


if __name__ == "__main__":
    unittest.main()
