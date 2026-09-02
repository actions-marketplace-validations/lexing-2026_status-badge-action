import unittest

from generate import _srcset_url, extract_dark_logo_url


LIGHT_LOGO = "https://cdn.example.test/logos/light.png"
DARK_LOGO = "https://cdn.example.test/logos/dark.png"


class LogoSourceTests(unittest.TestCase):
    def test_srcset_unwraps_better_stack_cdn_url(self):
        srcset = (
            "https://betterstack.com/cdn-cgi/image/width=200/"
            "https://cdn.example.test/logos/dark.png, "
            "https://betterstack.com/cdn-cgi/image/width=400/"
            "https://cdn.example.test/logos/dark.png 2x"
        )
        self.assertEqual(DARK_LOGO, _srcset_url(srcset))

    def test_dark_source_is_paired_with_light_source(self):
        page = f"""
        <picture>
          <source media="(prefers-color-scheme: dark)"
            srcset="https://cdn.example.test/other-dark.png">
          <source media="(prefers-color-scheme: light)"
            srcset="https://cdn.example.test/other-light.png">
          <img src="https://cdn.example.test/other-light.png">
        </picture>
        <picture>
          <source media="(prefers-color-scheme: light)"
            srcset="{LIGHT_LOGO}">
          <source media="(prefers-color-scheme: dark)"
            srcset="{DARK_LOGO}">
          <img src="{LIGHT_LOGO}">
        </picture>
        """
        self.assertEqual(DARK_LOGO, extract_dark_logo_url(page, LIGHT_LOGO))

    def test_missing_paired_dark_source_returns_none(self):
        page = f"""
        <picture>
          <source media="(prefers-color-scheme: light)"
            srcset="{LIGHT_LOGO}">
          <img src="{LIGHT_LOGO}">
        </picture>
        """
        self.assertIsNone(extract_dark_logo_url(page, LIGHT_LOGO))


if __name__ == "__main__":
    unittest.main()
