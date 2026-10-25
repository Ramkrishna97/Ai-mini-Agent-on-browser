def calculate_average(numbers):
    """Calculates the average of a list of numbers.

    Args:
        numbers: A list of numbers to calculate the average from.

    Returns:
        The average of the numbers in the list, or None if the list is empty.
    """
    if not numbers:
        return None
    return sum(numbers) / len(numbers)

if __name__ == '__main__':
    numbers1 = [1, 2, 3, 4, 5]
    average1 = calculate_average(numbers1)
    print(f"The average of {numbers1} is: {average1}")

    numbers2 = []
    average2 = calculate_average(numbers2)
    print(f"The average of {numbers2} is: {average2}")

    numbers3 = [10, 20, 30]
    average3 = calculate_average(numbers3)
    print(f"The average of {numbers3} is: {average3}")
