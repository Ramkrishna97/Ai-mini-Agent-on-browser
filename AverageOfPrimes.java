public final class AverageOfPrimes {
    private AverageOfPrimes() {
    }

    private static boolean isPrime(int value) {
        if (value < 2) {
            return false;
        }
        for (int divisor = 2; divisor <= value / divisor; divisor++) {
            if (value % divisor == 0) {
                return false;
            }
        }
        return true;
    }

    public static void main(String[] args) {
        final int count = 6;
        int found = 0;
        int sum = 0;
        int candidate = 3;
        StringBuilder primes = new StringBuilder();

        while (found < count) {
            if (isPrime(candidate)) {
                if (found > 0) {
                    primes.append(", ");
                }
                primes.append(candidate);
                sum += candidate;
                found++;
            }
            candidate += 2;
        }

        System.out.println("First six odd primes: " + primes);
        System.out.println("Average: " + (sum / (double) count));
    }
}
