import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;

public class DateTimeChecker {
    public static void main(String[] args) {
        // Get current date and time
        LocalDateTime now = LocalDateTime.now();
        
        // Format current date/time for display
        DateTimeFormatter formatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
        String currentTime = now.format(formatter);
        
        // Calculate tomorrow's date (same day, next day)
        LocalDateTime tomorrow = now.plusDays(1);
        
        // Calculate hours left until tomorrow starts
        long hoursLeft = java.time.Duration.between(now, tomorrow).toHours();
        
        System.out.println("Current Date and Time: " + currentTime);
        System.out.println("Tomorrow's Date: " + tomorrow.format(formatter));
        System.out.println("Hours left until tomorrow starts: " + Math.round(hoursLeft) + " hours");
    }
}
