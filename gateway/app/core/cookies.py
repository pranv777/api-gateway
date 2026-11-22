from http.cookiejar import DefaultCookiePolicy


class RejectAllCookiesPolicy(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False
