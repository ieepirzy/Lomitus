def slugify(s): return s.lower().replace(" ", "-")
def truncate(s, n): return s[:n] + "..." if len(s) > n else s
def capitalize_words(s): return " ".join(w.capitalize() for w in s.split())
def reverse(s): return s[::-1]
def pad(s, n): return s.rjust(n)
