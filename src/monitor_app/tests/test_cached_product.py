from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from monitor_app.cached_product import get_product


class CachedProductTests(SimpleTestCase):
    @patch('monitor_app.cached_product._background_build')
    @patch('monitor_app.cached_product._claim', return_value=True)
    @patch('monitor_app.models.CachedProduct.objects.filter')
    def test_async_first_fill_returns_refreshing_shell(
            self, product_filter, _claim, background_build):
        product_filter.return_value.first.return_value = None
        builder = Mock()

        result = get_product(
            'snapper_series:v9:epicprod:focus:site:test:30d',
            builder,
            ttl_seconds=3600,
            async_first_fill=True,
        )

        self.assertIsNone(result['value'])
        self.assertTrue(result['refreshing'])
        builder.assert_not_called()
        background_build.assert_called_once()
