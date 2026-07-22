"""Variable interpolation for JSON configs. Supports {{key}} and {{random.gen}}."""
import re
import random
import string
import logging


class VariableResolver:
    """Resolve {{placeholder}} variables in JSON config values."""

    def __init__(self, profile: dict, log=None):
        self.profile = profile
        self.log = log or logging.getLogger(__name__)

    def resolve(self, value: str) -> str:
        """Replace {{key}} with profile values and {{random.gen}} with generated values."""
        if not isinstance(value, str):
            return value

        def _replace(match):
            key = match.group(1)
            # Random generators
            if key.startswith("random."):
                return self._random(key[7:])
            # Profile lookup
            return str(self.profile.get(key, match.group(0)))

        return re.sub(r'\{\{(.+?)\}\}', _replace, value)

    def _random(self, gen: str) -> str:
        """Handle {{random.xxx}} generators."""
        if gen == "name":
            return random.choice(["James","John","Robert","Michael","David","Alex","Chris","Sam"])
        if gen == "last_name":
            return random.choice(["Smith","Jones","Williams","Taylor","Brown","Johnson","Davies","Wilson"])
        if gen == "email":
            name = random.choice(["james","john","alex","david","sam"])
            domain = random.choice(["gmail.com","outlook.com","yahoo.com","hotmail.com"])
            return f"{name}{random.randint(10,999)}@{domain}"
        if gen == "password":
            chars = string.ascii_letters + string.digits
            specials = "!@#%&*"
            # Generate 10 alphanumeric + 1 guaranteed special + 1 mixed = 12 chars
            pw = ''.join(random.choice(chars) for _ in range(10))
            pw += random.choice(specials)
            pw += random.choice(chars + specials)
            # Shuffle so special char isn't always at position 10
            pw_list = list(pw)
            random.shuffle(pw_list)
            return ''.join(pw_list)
        if gen == "phone":
            return f"{random.randint(200,999)}{random.randint(100,999)}{random.randint(1000,9999)}"
        if gen == "ssn":
            return ''.join(str(random.randint(0,9)) for _ in range(9))
        if gen == "zip":
            return random.choice(["90001","77001","33101","10001","60601","30301","44101","85001"])
        if gen == "dob":
            y = random.randint(1950, 2005)
            m = random.randint(1, 12)
            d = random.randint(1, 28)
            return f"{m:02d}/{d:02d}/{y}"
        if gen == "dob_month":
            return f"{random.randint(1,12):02d}"
        if gen == "dob_day":
            return f"{random.randint(1,28):02d}"
        if gen == "dob_year":
            return str(random.randint(1950, 2005))
        if gen.startswith("choice:"):
            opts = gen[7:].split(",")
            return random.choice(opts).strip()
        return f"{{{{random.{gen}}}}}"

    def resolve_dict(self, obj: dict) -> dict:
        """Recursively resolve all string values in a dict."""
        result = {}
        for k, v in obj.items():
            if isinstance(v, str):
                result[k] = self.resolve(v)
            elif isinstance(v, dict):
                result[k] = self.resolve_dict(v)
            elif isinstance(v, list):
                result[k] = [
                    self.resolve(i) if isinstance(i, str) else
                    self.resolve_dict(i) if isinstance(i, dict) else i
                    for i in v
                ]
            else:
                result[k] = v
        return result
