# Costinel / frontend

**Highest-Value Incremental Improvement:**

**Task:** Improve Cost Analytics & Visibility by adding a "Daily Cost Tracker" feature.

**Implementation Plan:**

1. **Research:** Review existing Cost Analytics & Visibility features to understand the current implementation and identify areas for improvement.
2. **Design:** Design a new feature that allows users to track daily costs in real-time. This feature should include:
	* A calendar view to select a date range.
	* A table to display daily costs for each cloud provider (AWS, GCP, Azure).
	* A chart to visualize daily costs over time.
3. **Implementation:**
	* Create a new API endpoint to retrieve daily cost data from the cloud providers.
	* Implement the calendar view and table using a library like FullCalendar and DataTables.
	* Use a charting library like Chart.js to visualize daily costs.
4. **Testing:** Test the new feature to ensure it works correctly and provides accurate data.
5. **Deployment:** Deploy the new feature to the production environment.

**Code Snippets:**

**API Endpoint:**
```python
from flask import Blueprint, jsonify
from costinel.services import get_daily_cost_data

daily_cost_tracker_blueprint = Blueprint('daily_cost_tracker', __name__)

@daily_cost_tracker_blueprint.route('/daily-cost-tracker', methods=['GET'])
def get_daily_cost_tracker():
    date_range = request.args.get('date_range')
    cloud_providers = request.args.get('cloud_providers')
    daily_cost_data = get_daily_cost_data(date_range, cloud_providers)
    return jsonify(daily_cost_data)
```

**Calendar View:**
```html
<div id="calendar"></div>
<script>
  $(document).ready(function() {
    $('#calendar').fullCalendar({
      header: {
        left: 'prev,next today',
        center: 'title',
        right: 'month,agendaWeek,agendaDay'
      },
      editable: true,
      events: function(start, end, timezone, callback) {
        $.ajax({
          type: 'GET',
          url: '/daily-cost-tracker',
          data: {
            date_range: start.format('YYYY-MM-DD') + ' - ' + end.format('YYYY-MM-DD')
          },
          success: function(data) {
            callback(data);
          }
        });
      }
    });
  });
</script>
```

**Table:**
```html
<table id="daily-cost-table">
  <thead>
    <tr>
      <th>Date</th>
      <th>AWS</th>
      <th>GCP</th>
      <th>Azure</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>2023-03-01</td>
      <td>$100.00</td>
      <td>$50.00</td>
      <td>$200.00</td>
    </tr>
    <tr>
      <td>2023-03-02</td>
      <td>$120.00</td>
      <td>$60.00</td>
      <td>$220.00</td>
    </tr>
  </tbody>
</table>
<script>
  $(document).ready(function() {
    $('#daily-cost-table').DataTable({
      ajax: '/daily-cost-tracker',
      columns: [
        { data: 'date' },
        { data: 'aws' },
        { data: 'gcp' },
        { data: 'azure' }
      ]
    });
  });
</script>
```

**Chart:**
```html
<canvas id="daily-cost-chart"></canvas>
<script>
  $(document).ready(function() {
    var ctx = document.getElementById('daily-cost-chart').getContext('2d');
    var chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: ['2023-03-01', '2023-03-02', '2023-03-03'],
        datasets: [{
          label: 'Daily Costs',
          data: [100, 120, 150],
          backgroundColor: 'rgba(255, 99, 132, 0.2)',
          borderColor: 'rgba(255, 99, 132, 1)',
          borderWidth: 1
        }]
      },
      options: {
        scales: {
          yAxes: [{
            ticks: {
              beginAtZero: true
            }
          }]
        }
      }
    });
  });
</script>
```
This is a high-level implementation plan for the Daily Cost Tracker feature. The actual implementation may vary depending on the specific requirements and existing codebase.
