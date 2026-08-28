class browser_config:
    @staticmethod
    def get_random_browser_config(browser_type):
        # Patchright owns the native Chromium identity. Returning no UA/CH
        # overrides prevents the solver from downgrading Chrome 151 to a
        # synthetic legacy Windows profile.
        if str(browser_type or "").strip().lower() == "chromium":
            return "chromium", "151.0.7922.34", None, None
        return str(browser_type or "chrome"), "native", None, None

    @staticmethod
    def get_browser_config(name, version):
        del name, version
        return None, None
