class BudgetExhausted(Exception):
    """No page actions left in the named scope ("url" or "run")."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"{scope} action budget exhausted")
        self.scope = scope


class ActionBudget:
    """Counts every page action (opening a page, each scroll gesture, each click).

    One counter for the run and one for the URL being processed. Callers spend
    before acting, so an exhausted budget stops the action from happening.
    """

    def __init__(self, run_budget: int, url_budget: int) -> None:
        self.run_budget = run_budget
        self.url_budget = url_budget
        self.run_spent = 0
        self.url_spent = 0
        self._in_url = False

    @property
    def url_remaining(self) -> int:
        return self.url_budget - self.url_spent

    @property
    def run_remaining(self) -> int:
        return self.run_budget - self.run_spent

    def can_start_url(self) -> bool:
        """True when the rest of the run budget covers a full per-URL budget."""
        return self.run_remaining >= self.url_budget

    def start_url(self) -> None:
        self._in_url = True
        self.url_spent = 0

    def end_url(self) -> int:
        """Stop counting for the URL and return the actions it used."""
        self._in_url = False
        return self.url_spent

    def spend(self, actions: int = 1) -> None:
        if self._in_url and self.url_spent + actions > self.url_budget:
            raise BudgetExhausted("url")
        if self.run_spent + actions > self.run_budget:
            raise BudgetExhausted("run")
        self.run_spent += actions
        if self._in_url:
            self.url_spent += actions
