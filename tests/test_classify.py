import pytest

from livejobs import classify as c


@pytest.mark.parametrize(
    "location,title,want",
    [
        ("Remote", "Engineer", "remote"),
        ("Remote - US", "Engineer", "remote"),
        ("New York", "Engineer (Remote)", "remote"),
        ("Work from home", "Support", "remote"),
        ("Hybrid / Remote", "Engineer", "hybrid"),
        ("Berlin (hybrid)", "Engineer", "hybrid"),
        ("Berlin", "Engineer", ""),
        ("Austin, TX (non-remote)", "Engineer", ""),
        ("On-site, remote kitchen", "Cook", ""),
        ("", "", ""),
        ("Remotely", "x", ""),  # whole word only
    ],
)
def test_workplace(location, title, want):
    assert c.workplace(location, title) == want


@pytest.mark.parametrize(
    "title,want",
    [
        ("Software Engineering Intern", "intern"),
        ("Werkstudent Marketing", "intern"),
        ("Junior Data Analyst", "entry"),
        ("Associate Product Manager", "entry"),
        ("Associate Director, Finance", "executive"),
        ("Graduate Software Engineer", "entry"),
        ("Software Engineer I", "entry"),
        ("Software Engineer II", "mid"),
        ("Software Engineer III", "senior"),
        ("Senior Software Engineer", "senior"),
        ("Sr. Accountant", "senior"),
        ("Staff Software Engineer", "lead"),
        ("Principal Scientist", "lead"),
        ("Engineering Manager", "lead"),
        ("Senior Manager, Sales", "lead"),
        ("Staff Accountant", "mid"),
        ("Director of Engineering", "executive"),
        ("Senior Director, Finance", "executive"),
        ("VP Sales", "executive"),
        ("Chief Technology Officer", "executive"),
        ("Head of Design", "executive"),
        ("Software Engineer", "mid"),
        ("Internal Auditor", "mid"),  # 'Internal' is not 'Intern'
        ("International Sales Rep", "mid"),
        ("", "mid"),
    ],
)
def test_seniority(title, want):
    assert c.seniority(title) == want


@pytest.mark.parametrize(
    "location,want",
    [
        ("San Francisco, CA", "US"),
        ("Austin, Texas", "US"),
        ("Remote - US", "US"),
        ("United States", "US"),
        ("Dublin, OH", "US"),
        ("Toronto, ON", "CA"),
        ("Vancouver, BC, Canada", "CA"),
        ("London, United Kingdom", "GB"),
        ("London", "GB"),
        ("Berlin, Germany", "DE"),
        ("Munich", "DE"),
        ("Bengaluru, India", "IN"),
        ("Paris", "FR"),
        ("Remote", ""),
        ("", ""),
        ("Somewhere unknown", ""),
        ("Sydney, NSW, Australia", "AU"),
    ],
)
def test_country(location, want):
    assert c.country(location) == want


@pytest.mark.parametrize(
    "names,want",
    [
        (("Acme Staffing Solutions",), True),
        (("Global Recruitment Partners",), True),
        (("randstad-usa",), True),
        (("robert-half",), True),
        (("Talent Acquisition Group",), True),
        (("Executive Search Associates",), True),
        (("Staffbase",), False),  # a software company, not an agency
        (("Search Engine Co",), False),
        (("Stripe",), False),
        (("Shaysville Labs",), False),  # 'hays' inside a word
        (("", ""), False),
        (("x", "Allegis Group"), True),
    ],
)
def test_is_agency(names, want):
    assert c.is_agency(*names) is want
