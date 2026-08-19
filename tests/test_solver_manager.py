from unittest.mock import patch

from services import solver_manager


def test_solver_pool_supports_registration_nodes_full_fifteen_concurrency():
    with patch.object(solver_manager, "_runtime_solver_value", return_value="15"):
        assert solver_manager._solver_max_browsers() == 15

    with patch.object(solver_manager, "_runtime_solver_value", return_value="999"):
        assert solver_manager._solver_max_browsers() == 15
