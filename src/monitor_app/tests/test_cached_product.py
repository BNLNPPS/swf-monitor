from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from monitor_app.cached_product import get_product


class CachedProductTests(SimpleTestCase):
    @patch('monitor_app.cached_product.stores', return_value=True)
    @patch('monitor_app.cached_product._background_build')
    @patch('monitor_app.cached_product._claim', return_value=True)
    @patch('monitor_app.models.CachedProduct.objects.filter')
    def test_async_first_fill_returns_refreshing_shell(
            self, product_filter, _claim, background_build, _stores):
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

    @patch('monitor_app.cached_product.stores', return_value=False)
    @patch('monitor_app.cached_product._claim')
    @patch('monitor_app.models.CachedProduct.objects.filter')
    def test_development_tree_serves_stale_and_never_writes(
            self, product_filter, claim, _stores):
        from datetime import timedelta

        from django.utils import timezone
        row = Mock(value={'v': 1}, built_at=timezone.now() - timedelta(hours=1))
        product_filter.return_value.first.return_value = row
        builder = Mock(return_value={'v': 2})

        stale = get_product('comp_snapshot:v1', builder, ttl_seconds=60)
        self.assertEqual(stale['value'], {'v': 1})
        self.assertFalse(stale['refreshing'])
        builder.assert_not_called()

        fresh = get_product('comp_snapshot:v1', builder, ttl_seconds=60, refresh=True)
        self.assertEqual(fresh['value'], {'v': 2})
        claim.assert_not_called()

    def test_release_and_development_paths(self):
        from monitor_app import cached_product
        self.assertEqual(cached_product.stores(), cached_product.__file__.startswith(
            __import__('os').path.realpath(cached_product.RELEASE_ROOT)))
