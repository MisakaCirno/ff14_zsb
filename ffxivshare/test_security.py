from django.test import SimpleTestCase, override_settings


class ContentSecurityPolicyTests(SimpleTestCase):
    def test_default_policy_is_report_only_and_self_contained(self):
        response = self.client.get('/health/live/')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Content-Security-Policy', response)
        policy = response['Content-Security-Policy-Report-Only']
        self.assertIn("default-src 'self'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn('http:', policy)
        self.assertNotIn('https:', policy)

    @override_settings(CSP_REPORT_ONLY=False)
    def test_policy_can_be_enforced_after_compatibility_observation(self):
        response = self.client.get('/health/live/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Content-Security-Policy', response)
        self.assertNotIn('Content-Security-Policy-Report-Only', response)
