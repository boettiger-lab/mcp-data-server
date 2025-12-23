kubectl create secret generic wetlands-outputs \
  --from-literal=AWS_ACCESS_KEY_ID="$OUTPUT_KEY" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$OUTPUT_SECRET" \
  -n biodiversity