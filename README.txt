DELIVERY ROUTE OPTIMIZER

1. Install the required packages once:
   python -m pip install -r requirements.txt

2. Start the app:
   python -m streamlit run app.py

How to use it
-------------
* Paste your start point and every parcel location. Each box accepts a latitude,
  longitude pair, labelled coordinates, a Google Maps link containing coordinates,
  or a complete address.
* Tap Check locations. The app shows the chosen point on a map and provides an
  Open in Google Maps button for every location. Verify it before route planning.
* Tap Find best road route to calculate the delivery sequence, road distance, and
  driving-time estimate. Up to eight stops are optimized exactly; more use a fast
  nearest-stop method.

Free by default
---------------
Addresses are resolved with OpenStreetMap/Nominatim and routes use the public OSRM
service. No Google API key is needed for coordinates, Google Maps verification links,
or the free prototype. Add the city, area, and PIN code when pasting an address.

Google geocoding is optional in the sidebar. It requires your own billing-enabled
Google Geocoding API key, so it is not needed for normal prototype use.

Note: the app labels any fallback estimate clearly if the free road-routing service is
temporarily unavailable. Always verify the result with the Google Maps button before
heading to a customer.
