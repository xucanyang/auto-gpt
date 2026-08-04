import threading
import unittest

from core.chatgpt_register_exit_ip_registry import (
    RegisterExitIPRegistry,
    normalize_register_exit_ip,
)


class RegisterExitIPRegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.registry = RegisterExitIPRegistry(clock=lambda: self.now[0])

    def test_atomic_claim_allows_only_one_owner(self):
        barrier = threading.Barrier(3)
        claims = []
        lock = threading.Lock()

        def claim(owner: str) -> None:
            barrier.wait(timeout=2)
            result = self.registry.claim(
                "203.0.113.8",
                owner=owner,
                active_ttl_seconds=60,
            )
            with lock:
                claims.append(result)

        threads = [
            threading.Thread(target=claim, args=("task-a:1",)),
            threading.Thread(target=claim, args=("task-b:1",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(sum(1 for result in claims if result.claimed), 1)
        self.assertEqual(sum(1 for result in claims if not result.claimed), 1)
        rejected = next(result for result in claims if not result.claimed)
        self.assertEqual(rejected.state, "active")
        self.assertGreater(rejected.expires_in_seconds, 0)

    def test_release_moves_claim_to_cooldown_until_expiry(self):
        first = self.registry.claim(
            "203.0.113.9",
            owner="task-a:1",
            active_ttl_seconds=60,
        )
        self.assertTrue(first.claimed)
        self.assertEqual(
            self.registry.release_owner("task-a:1", cooldown_seconds=30),
            1,
        )

        cooling = self.registry.claim(
            "203.0.113.9",
            owner="task-b:1",
            active_ttl_seconds=60,
        )
        self.assertFalse(cooling.claimed)
        self.assertEqual(cooling.state, "cooldown")

        self.now[0] += 31
        after_expiry = self.registry.claim(
            "203.0.113.9",
            owner="task-b:1",
            active_ttl_seconds=60,
        )
        self.assertTrue(after_expiry.claimed)

    def test_ipv6_addresses_are_deduplicated_by_64_network(self):
        first_ip = "2001:db8:abcd:12::1"
        second_ip = "2001:db8:abcd:12::ffff"
        self.assertEqual(
            normalize_register_exit_ip(first_ip),
            "2001:db8:abcd:12::/64",
        )
        self.assertEqual(
            normalize_register_exit_ip(second_ip),
            "2001:db8:abcd:12::/64",
        )

        first = self.registry.claim(
            first_ip,
            owner="task-a:1",
            active_ttl_seconds=60,
        )
        second = self.registry.claim(
            second_ip,
            owner="task-b:1",
            active_ttl_seconds=60,
        )
        self.assertTrue(first.claimed)
        self.assertFalse(second.claimed)
        self.assertEqual(second.key, "2001:db8:abcd:12::/64")

    def test_same_owner_can_refresh_active_lease(self):
        self.assertTrue(
            self.registry.claim(
                "203.0.113.10",
                owner="task-a:1",
                active_ttl_seconds=10,
            ).claimed
        )
        self.now[0] += 5
        refreshed = self.registry.claim(
            "203.0.113.10",
            owner="task-a:1",
            active_ttl_seconds=20,
        )
        self.assertTrue(refreshed.claimed)
        self.assertAlmostEqual(
            self.registry.snapshot()["203.0.113.10"]["expires_in_seconds"],
            20,
        )

    def test_refresh_owner_extends_active_lease_until_new_expiry(self):
        self.assertTrue(
            self.registry.claim(
                "203.0.113.11",
                owner="task-a:1",
                active_ttl_seconds=10,
            ).claimed
        )
        self.now[0] += 5
        self.assertEqual(
            self.registry.refresh_owner("task-a:1", active_ttl_seconds=20),
            1,
        )

        self.now[0] += 19
        still_active = self.registry.claim(
            "203.0.113.11",
            owner="task-b:1",
            active_ttl_seconds=10,
        )
        self.assertFalse(still_active.claimed)
        self.assertEqual(still_active.state, "active")

        self.now[0] += 2
        after_expiry = self.registry.claim(
            "203.0.113.11",
            owner="task-b:1",
            active_ttl_seconds=10,
        )
        self.assertTrue(after_expiry.claimed)

    def test_ipv6_claim_exposes_canonical_key_for_task_lifetime_tracking(self):
        claim = self.registry.claim(
            "2001:db8:abcd:44::1234",
            owner="task-a:1",
            active_ttl_seconds=60,
        )

        self.assertTrue(claim.claimed)
        self.assertEqual(claim.key, "2001:db8:abcd:44::/64")


if __name__ == "__main__":
    unittest.main()
