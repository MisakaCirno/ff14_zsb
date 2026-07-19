from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Attach the site's fixed CSP in report-only or enforced mode."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        report_only = getattr(settings, 'CSP_REPORT_ONLY', True)
        header_name = (
            'Content-Security-Policy-Report-Only'
            if report_only
            else 'Content-Security-Policy'
        )
        response.headers[header_name] = settings.CONTENT_SECURITY_POLICY
        return response
