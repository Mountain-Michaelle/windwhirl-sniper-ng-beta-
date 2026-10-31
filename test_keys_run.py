import hashlib

def lok(o):
    p=[str(o.get("customer_name","")).strip().lower(),
       str(o.get("phone_number",o.get("phone",""))).strip(),
       str(o.get("product","")).strip().lower(),
       str(o.get("price","")).strip()]
    return hashlib.sha1("|".join(p).encode("utf-8")).hexdigest()[:16]

def pok(cid,o):
    p=[str(o.get("customer_name","")).strip().lower(),
       str(o.get("phone_number",o.get("phone","")).strip()),
       str(o.get("product","")).strip().lower(),
       str(o.get("price","")).strip()]
    return hashlib.sha1("|".join([cid]+p).encode("utf-8")).hexdigest()[:16]

o={"customer_name":"JANE DOE","phone_number":"08012345678","product":"Sadoer Collagen Serum","price":"29500","address":"123 Lagos Street"}
lk=lok(o)
pk1=pok("client_jane_97a9624c",o)
pk2=pok("client_alex_fd856e4c",o)
print(f"Local: {lk}")
print(f"Prod Jane: {pk1}")
print(f"Prod Alex: {pk2}")
print(f"Local!=Prod: {lk!=pk1}")
print(f"Jane!=Alex: {pk1!=pk2}")
print(f"Deterministic: {pok(\"client_jane_97a9624c\",o)==pok(\"client_jane_97a9624c\",o)}")