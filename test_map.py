"""The map: Members as rough ~5 km squares, from postal codes."""

from __future__ import annotations

import io
import os
import zipfile

import geo
from testkit import Join

POSTAL = "\n".join(
    [
        "US\t14850\tIthaca\tNew York\tNY\tTompkins\t109\t\t\t42.4422\t-76.4949\t4",
        "US\t14886\tTrumansburg\tNew York\tNY\tTompkins\t109\t\t\t42.5423\t-76.6661\t4",
        "GB\tSW1A\tLondon\tEngland\tENG\t\t\t\t\t51.5010\t-0.1416\t4",
        "CA\tK1A\tOttawa\tOntario\tON\t\t\t\t\t45.4215\t-75.6972\t4",
    ]
)
COUNTRIES = "#ISO\tISO3\tnum\tfips\tCountry\n" + "\n".join(
    ["US\tUSA\t840\tUS\tUnited States", "GB\tGBR\t826\tUK\tUnited Kingdom", "CA\tCAN\t124\tCA\tCanada"]
)


def _Zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("allCountries.txt", POSTAL + "\n")
        archive.writestr("readme.txt", "test")
    return buffer.getvalue()


def test_locator_finds_codes_countries_and_towns(tmp_path):
    path = geo.Build(str(tmp_path), _Zip(), COUNTRIES)
    locator = geo.Locator(path)
    assert locator.Locate("United States", "14850") == (42.442, -76.495)
    assert locator.Locate("USA", "14850-1234") == (42.442, -76.495)  # ZIP+4 falls back to the 5 digits
    assert locator.Locate("UK", "SW1A 1AA") == (51.501, -0.142)  # only the first part is listed
    assert locator.Locate("Canada", "K1A 0B1") == (45.422, -75.697)
    assert locator.Locate("United States", "99999", "Trumansburg") == (42.542, -76.666)
    assert locator.Locate("Atlantis", "12345") is None


def test_rough_spot_hides_the_exact_place():
    a = geo.RoughSpot(42.4422, -76.4949)
    b = geo.RoughSpot(42.4430, -76.4960)  # a few streets away
    assert a == b
    assert abs(a[0] - 42.4422) < 0.045 and abs(a[1] - -76.4949) < 0.07


def test_map_page_shows_towns_and_counts_but_no_names(client):
    test_client, app_module = client
    page = test_client.get("/map").data.decode()
    assert "isn't ready yet" in page
    geo.Build(os.path.dirname(app_module.GEO_PATH), _Zip(), COUNTRIES)
    Join(test_client, app_module, "janedoe", "Jane Doe")  # testkit registers Jane in Ithaca, 14850
    test_client.get("/logout")
    page = test_client.get("/map").data.decode()
    assert "Ithaca" in page and "1 Member" in page
    assert "Jane Doe" not in page
