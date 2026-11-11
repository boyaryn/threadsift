import pytest

from threadsift.collection.budget import ActionBudget, BudgetExhausted


def test_actions_count_against_url_and_run() -> None:
    budget = ActionBudget(run_budget=1200, url_budget=150)
    budget.start_url()
    for _ in range(10):
        budget.spend()
    budget.spend(5)
    assert budget.url_spent == 15
    assert budget.run_spent == 15
    assert budget.end_url() == 15


def test_warmup_actions_count_only_against_the_run() -> None:
    budget = ActionBudget(1200, 150)
    for _ in range(40):
        budget.spend()
    assert (budget.run_spent, budget.url_spent) == (40, 0)
    budget.start_url()
    assert budget.url_spent == 0
    budget.spend()
    assert (budget.run_spent, budget.url_spent) == (41, 1)


def test_url_counter_resets_for_each_url() -> None:
    budget = ActionBudget(1200, 150)
    budget.start_url()
    budget.spend(100)
    assert budget.end_url() == 100
    budget.start_url()
    assert budget.url_spent == 0
    budget.spend(10)
    assert budget.end_url() == 10
    assert budget.run_spent == 110


def test_url_budget_is_exhausted_before_the_action_happens() -> None:
    budget = ActionBudget(1200, 150)
    budget.start_url()
    budget.spend(150)
    assert budget.url_remaining == 0
    with pytest.raises(BudgetExhausted) as stopped:
        budget.spend()
    assert stopped.value.scope == "url"
    assert (budget.url_spent, budget.run_spent) == (150, 150)


def test_spending_just_inside_the_budget_is_allowed() -> None:
    budget = ActionBudget(1200, 150)
    budget.start_url()
    budget.spend(149)
    budget.spend(1)
    with pytest.raises(BudgetExhausted):
        budget.spend(1)


def test_not_enough_run_budget_for_another_url() -> None:
    budget = ActionBudget(run_budget=1200, url_budget=150)
    budget.spend(1100)  # for example the warm-up and earlier URLs
    assert budget.run_remaining == 100
    assert not budget.can_start_url()


def test_run_budget_that_exactly_covers_a_url_is_enough() -> None:
    budget = ActionBudget(1200, 150)
    budget.spend(1050)
    assert budget.can_start_url()
    budget.start_url()
    budget.spend(150)  # the URL can use its whole budget without cutting the run off
    assert budget.run_spent == 1200


def test_full_urls_never_exceed_the_run_budget() -> None:
    budget = ActionBudget(1200, 150)
    budget.spend(30)  # warm-up
    processed = 0
    while budget.can_start_url():
        budget.start_url()
        budget.spend(150)
        budget.end_url()
        processed += 1
    assert processed == 7
    assert budget.run_spent == 1080 and budget.run_spent <= 1200


def test_run_budget_is_also_enforced_directly() -> None:
    budget = ActionBudget(run_budget=10, url_budget=5)
    budget.spend(10)
    with pytest.raises(BudgetExhausted) as stopped:
        budget.spend()
    assert stopped.value.scope == "run"
