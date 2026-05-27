import requests

print("--- Testing default clean request ---")
url = "https://api.openalex.org/sources?filter=issn:0007-9235"
r = requests.get(url)
print("Status:", r.status_code)
print("Response text:", r.text[:500])
