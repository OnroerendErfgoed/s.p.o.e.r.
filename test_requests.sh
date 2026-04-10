#!/bin/bash
# ============================================================================
# Test requests for the Toelatingen POC
# Uses typed endpoints — no type or role needed in payload
# ============================================================================

BASE_URL="http://localhost:8000"

# ----------------------------------------------------------------------------
# File upload helper
# ----------------------------------------------------------------------------
# Usage: FID=$(upload_file <user> <local_filename> <display_filename>)
#
# Calls POST /files/upload/request to get a signed upload URL, then PUTs a
# small synthetic payload to the File Service. Echoes the resulting file_id
# (and only the file_id) on stdout so it can be captured.
#
# Errors are written to stderr; on failure the function returns the empty
# string and the caller's curl will produce a 422 from the engine.
upload_file() {
  local user="$1"
  local content="$2"
  local filename="$3"

  local resp
  resp=$(curl -s -X POST "$BASE_URL/files/upload/request" \
    -H "Content-Type: application/json" \
    -H "X-POC-User: $user" \
    -d "{\"filename\": \"$filename\"}")

  local file_id upload_url
  file_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])" 2>/dev/null)
  upload_url=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])" 2>/dev/null)

  if [ -z "$file_id" ] || [ -z "$upload_url" ]; then
    echo "upload_file: failed to get token: $resp" >&2
    return 1
  fi

  # PUT the bytes to the File Service. The endpoint expects multipart form
  # data with a `file` field.
  local tmpfile
  tmpfile=$(mktemp)
  printf '%s' "$content" > "$tmpfile"
  curl -s -X PUT "$upload_url" -F "file=@$tmpfile;filename=$filename" > /dev/null
  rm -f "$tmpfile"

  echo "$file_id"
}

# ----------------------------------------------------------------------------

echo "============================================"
echo "DOSSIER 1: Brugge, RRN aanvrager"
echo "============================================"
echo ""

echo "--- D1 Step 1: dienAanvraagIn (with bijlage) ---"
D1_BIJLAGE_FID=$(upload_file "jan.aanvrager" "Detailplan voor de gevelrestauratie." "detailplan.pdf")
echo "  uploaded bijlage file_id=$D1_BIJLAGE_FID"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10001\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"Restauratie gevelbekleding stadhuis\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"bijlagen\": [
            { \"file_id\": \"$D1_BIJLAGE_FID\", \"filename\": \"detailplan.pdf\", \"content_type\": \"application/pdf\", \"size\": 32 }
          ]
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Verify file_download_url injection ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
found = False
for e in d.get('currentEntities', []):
    if e['type'] == 'oe:aanvraag':
        bs = e['content'].get('bijlagen', [])
        assert bs, f'aanvraag has no bijlagen: {e[\"content\"]}'
        for b in bs:
            assert 'file_download_url' in b, f'missing file_download_url on bijlage: {b}'
            print(f\"  bijlage file_id={b['file_id'][:8]}... file_download_url={b['file_download_url'][:70]}...\")
            found = True
        break
assert found, 'no oe:aanvraag entity found in currentEntities'
print('  OK: file_download_url was injected on Bijlage.file_id')
"
echo ""

echo "--- D1 Step 2: neemBeslissing (onvolledig, direct) ---"
D1_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief: aanvraag onvolledig." "d1-brief-001.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000002/neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"onvolledig\",
          \"datum\": \"2026-03-26T10:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"brief\": \"$D1_BRIEF1_FID\"
        }
      },
      {
        \"entity\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003\",
        \"content\": { \"getekend\": true }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D1 Verify brief_download_url injection (default naming rule) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
found = False
for e in d.get('currentEntities', []):
    if e['type'] == 'oe:beslissing':
        c = e['content']
        assert 'brief' in c, f'no brief in beslissing: {c}'
        assert 'brief_download_url' in c, f'missing brief_download_url: keys={sorted(c.keys())}'
        print(f\"  brief={c['brief'][:8]}... brief_download_url={c['brief_download_url'][:70]}...\")
        found = True
        break
assert found, 'no oe:beslissing entity found'
print('  OK: brief_download_url was injected on Beslissing.brief (default rule)')
"
echo ""

echo "--- D1 Step 3: vervolledigAanvraag ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000004/vervolledigAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/10001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Restauratie gevelbekleding stadhuis - aangevuld met detailplannen",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D1 Step 4: neemBeslissing (goedgekeurd, direct) ---"
D1_BRIEF2_FID=$(upload_file "marie.brugge" "Beslissingsbrief: aanvraag goedgekeurd." "d1-brief-002.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000005/neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000005\",
        \"derivedFrom\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-27T14:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"brief\": \"$D1_BRIEF2_FID\"
        }
      },
      {
        \"entity\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000006\",
        \"derivedFrom\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003\",
        \"content\": { \"getekend\": true }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D1 Graph: $BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""


echo "============================================"
echo "DOSSIER 2: Gent, KBO aanvrager, separate signer, declined signing"
echo "  behandelaar: benjamma"
echo "  ondertekenaar: sophie.tekent"
echo "============================================"
echo ""

echo "--- D2 Step 1: dienAanvraagIn (firma.acme) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 2: doeVoorstelBeslissing — onvolledig (benjamma) ---"
D2_BRIEF1_FID=$(upload_file "benjamma" "Beslissingsbrief D2: voorstel onvolledig." "d2-brief-001.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000002/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"onvolledig\",
          \"datum\": \"2026-03-26T11:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF1_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 3: tekenBeslissing — sophie signs (triggers neemBeslissing → onvolledig) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000003/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D2 Step 4: vervolledigAanvraag (firma.acme) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000004/vervolledigAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand - met technische fiche",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 5: bewerkAanvraag (benjamma) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000005/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000005",
        "derivedFrom": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000004",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand - met technische fiche en advies",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 6: doeVoorstelBeslissing — goedgekeurd (benjamma) ---"
D2_BRIEF2_FID=$(upload_file "benjamma" "Beslissingsbrief D2: voorstel goedgekeurd." "d2-brief-002.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000006/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006\",
        \"derivedFrom\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-28T09:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF2_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 7: tekenBeslissing — sophie DECLINES (getekend: false → klaar_voor_behandeling) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000007/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000007",
        "derivedFrom": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": { "getekend": false }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Check status (expect: klaar_voor_behandeling) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D2 Step 8: doeVoorstelBeslissing — goedgekeurd second attempt (benjamma) ---"
D2_BRIEF3_FID=$(upload_file "benjamma" "Beslissingsbrief D2: tweede voorstel goedgekeurd." "d2-brief-003.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000008/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000008\",
        \"derivedFrom\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-29T10:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF3_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 9: tekenBeslissing — sophie SIGNS (triggers neemBeslissing → goedgekeurd) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000009/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000009",
        "derivedFrom": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000007",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D2 Graph: $BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""

echo "============================================"
echo "List all dossiers"
echo "============================================"
curl -s "$BASE_URL/dossiers" -H "X-POC-User: claeyswo" | python3 -m json.tool
echo ""
echo ""

echo "============================================"
echo "DOSSIER 3: Batch — bewerkAanvraag + doeVoorstelBeslissing in one call"
echo "============================================"
echo ""

echo "--- D3 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/activities/a3000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/30001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Batch test — renovatie kapel",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/30001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D3 Step 2: BATCH bewerkAanvraag + doeVoorstelBeslissing ---"
D3_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief D3: kapel renovatie." "d3-brief-001.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"activities\": [
      {
        \"activity_id\": \"a3000000-0000-0000-0000-000000000002\",
        \"type\": \"bewerkAanvraag\",
        \"used\": [
          { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/30001\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000002\",
            \"derivedFrom\": \"oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000001\",
            \"content\": {
              \"onderwerp\": \"Batch test — renovatie kapel (bewerkt met advies)\",
              \"handeling\": \"renovatie\",
              \"aanvrager\": { \"rrn\": \"85010100123\" },
              \"gemeente\": \"Brugge\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/30001\"
            }
          }
        ]
      },
      {
        \"activity_id\": \"a3000000-0000-0000-0000-000000000003\",
        \"type\": \"doeVoorstelBeslissing\",
        \"generated\": [
          {
            \"entity\": \"oe:beslissing/e3000000-0000-0000-0000-000000000002@f3000000-0000-0000-0000-000000000003\",
            \"content\": {
              \"beslissing\": \"goedgekeurd\",
              \"datum\": \"2026-03-30T12:00:00Z\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/30001\",
              \"brief\": \"$D3_BRIEF1_FID\"
            }
          }
        ]
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D3 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D3 Graph: $BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""

echo "============================================"
echo "DOSSIER 4: Batch — explicit used ref between activities"
echo "  bewerkAanvraag generates oe:aanvraag@new_version"
echo "  doeVoorstelBeslissing explicitly uses that version"
echo "============================================"
echo ""

echo "--- D4 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/activities/a4000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/40001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Explicit ref batch test — restauratie toren",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/40001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D4 Step 2: BATCH bewerkAanvraag + doeVoorstelBeslissing (explicit used ref) ---"
D4_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief D4: torenrestauratie." "d4-brief-001.pdf")
curl -s -X PUT "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"activities\": [
      {
        \"activity_id\": \"a4000000-0000-0000-0000-000000000002\",
        \"type\": \"bewerkAanvraag\",
        \"used\": [
          { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/40001\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002\",
            \"derivedFrom\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000001\",
            \"content\": {
              \"onderwerp\": \"Explicit ref batch test — restauratie toren (bewerkt)\",
              \"handeling\": \"renovatie\",
              \"aanvrager\": { \"rrn\": \"85010100123\" },
              \"gemeente\": \"Brugge\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/40001\"
            }
          }
        ]
      },
      {
        \"activity_id\": \"a4000000-0000-0000-0000-000000000003\",
        \"type\": \"doeVoorstelBeslissing\",
        \"used\": [
          { \"entity\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:beslissing/e4000000-0000-0000-0000-000000000002@f4000000-0000-0000-0000-000000000003\",
            \"content\": {
              \"beslissing\": \"goedgekeurd\",
              \"datum\": \"2026-03-30T14:00:00Z\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/40001\",
              \"brief\": \"$D4_BRIEF1_FID\"
            }
          }
        ]
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D4 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D4 Graph: $BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""

# ============================================================================
# DOSSIER 5: derivation rules — negative tests
# ============================================================================
# These cases deliberately trip the derivation validator added to the engine.
# Uses bewerkAanvraag for the revision step because it only requires
# klaar_voor_behandeling status, which is what we end up in after an initial
# dienAanvraagIn.
# ============================================================================

echo "============================================"
echo "DOSSIER 5: derivation rules — negative tests"
echo "============================================"
echo ""

D5_AANVRAAG_FID=$(upload_file "jan.aanvrager" "initiele aanvraag bijlage" "d5-initieel.pdf")

echo "--- D5 Step 1: dienAanvraagIn (baseline v1) ---"
curl -s -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [{\"entity\": \"https://id.erfgoed.net/erfgoedobjecten/50001\"}],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"Derivation test baseline\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/50001\",
          \"bijlagen\": [{ \"file_id\": \"$D5_AANVRAAG_FID\", \"filename\": \"d5-initieel.pdf\" }]
        }
      }
    ]
  }" > /dev/null
echo "  baseline aanvraag v1 created"
echo ""

echo "--- D5 Step 2: bewerkAanvraag v2 (happy path — correct derivedFrom from v1) ---"
curl -s -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000002/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Derivation test baseline - bewerkt v2",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }' | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'detail' in d:
    print(f'  FAIL: got error: {d[\"detail\"]}')
    sys.exit(1)
print('  OK: happy-path derivation v1->v2 accepted')
"
echo ""

echo "--- D5 Step 3: NEGATIVE — missing derivedFrom on existing entity (expect 409 missing_derivation) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000003/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000003",
        "content": {
          "onderwerp": "missing derivedFrom",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '409', f'expected 409, got {code}: {body}'
assert isinstance(inner, dict), f'expected dict detail, got {type(inner).__name__}: {inner}'
assert inner.get('error') == 'missing_derivation', f'expected error=missing_derivation, got {inner.get(\"error\")}'
assert 'latest_version' in inner, f'expected latest_version in payload'
lv = inner['latest_version']
assert lv['versionId'] == 'f5000000-0000-0000-0000-000000000002', f'wrong latest: {lv[\"versionId\"]}'
print(f'  OK: 409 missing_derivation; latest_version.versionId={lv[\"versionId\"][:8]}...')
"
echo ""

echo "--- D5 Step 4: NEGATIVE — stale derivedFrom (v1, but latest is v2) (expect 409 stale_derivation) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000004/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "stale derivation",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '409', f'expected 409, got {code}: {body}'
assert inner.get('error') == 'stale_derivation', f'expected error=stale_derivation, got {inner.get(\"error\")}'
assert inner.get('declared_parent') == 'f5000000-0000-0000-0000-000000000001'
assert inner.get('latest_parent') == 'f5000000-0000-0000-0000-000000000002'
assert 'latest_version' in inner
print(f'  OK: 409 stale_derivation; declared=v1, latest=v2, latest_version.content returned')
"
echo ""

echo "--- D5 Step 5: NEGATIVE — unknown parent version (expect 422 unknown_parent) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000005/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000006",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@ffffffff-ffff-ffff-ffff-ffffffffffff",
        "content": {
          "onderwerp": "unknown parent",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'unknown_parent', f'expected error=unknown_parent, got {inner.get(\"error\")}'
print(f'  OK: 422 unknown_parent')
"
echo ""

echo "--- D5 Step 6: NEGATIVE — cross-entity derivation (expect 422 cross_entity_derivation) ---"
# NEW entity_id (e5...99) trying to derive from the existing e5...01 chain
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000006/bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000099@f5000000-0000-0000-0000-000000000099",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002",
        "content": {
          "onderwerp": "cross-entity derivation",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'cross_entity_derivation', f'expected error=cross_entity_derivation, got {inner.get(\"error\")}'
print(f'  OK: 422 cross_entity_derivation')
"
echo ""

echo "D5 summary: all 5 derivation rule checks passed"
