You are extracting telecom contract and service information from an email message.

Emails from carriers or about carrier services may contain:
- Contract confirmations with terms, dates, rates
- Service order confirmations with circuit IDs, phone numbers, service types
- Rate quotes with monthly charges per service
- LOA (Letter of Agency) confirmations
- Account information (account numbers, customer names)

WHAT TO EXTRACT:
- Account numbers and customer names mentioned in the email
- Contract terms (months), effective dates, expiration dates
- Monthly recurring charges and service descriptions
- Phone numbers, circuit IDs associated with the services
- Contract/quote/order numbers
- Auto-renew or month-to-month language

EXTRACTION RULES:
- Extract ONLY what is explicitly stated in the email body
- Ignore email signatures, disclaimers, and confidentiality notices
- If the email is a forwarded chain, extract from the most recent relevant message
- "LOA" emails may confirm carrier changes — extract the carrier and account info
- Rate quotes: each service line = one row
- If the email just confirms receipt or is purely administrative with no service/contract data, return an empty array []
